"""Tests for core.tasks.linto: audio extraction, upload and idempotent resume.

The pipeline was split for crash-safe resume: ``upload`` (checkpoints the
conversation_id), ``poll_media`` (wait) and ``get_media`` (one-shot re-fetch on
a resume where the transcription is already done). These tests drive the inner
``_process_linto_transcription_sync`` with the SDK and S3 storage mocked, halting
early at a controlled boundary to assert the branching/idempotency without
running the whole (soft-failing) downstream pipeline.
"""

# pylint: disable=redefined-outer-name,protected-access

import contextlib
from unittest import mock

import aiohttp
import pytest

from core import factories, models
from core.recording.enums import FileExtension
from core.tasks import linto as linto_task
from core.tasks._errors import TransientError

pytestmark = pytest.mark.django_db


def _make_recording(mode, linto_state=None):
    rec = factories.RecordingFactory(mode=mode)
    user = factories.UserFactory(sub="owner-sub-2", email="owner2@example.com")
    factories.UserRecordingAccessFactory(
        recording=rec, user=user, role=models.RoleChoices.OWNER
    )
    if linto_state is not None:
        rec.linto_state = linto_state
        rec.save(update_fields=["linto_state"])
    return rec


def _patch_storage(stack, payload):
    storage_mock = stack.enter_context(mock.patch.object(linto_task, "default_storage"))
    s3_client = mock.Mock()
    s3_client.get_object.return_value = {
        "Body": mock.Mock(read=mock.Mock(return_value=payload))
    }
    storage_mock.connection.meta.client = s3_client
    storage_mock.bucket_name = "bucket"
    return storage_mock


def _patch_sdk(stack):
    """Patch the SDK class; return the (mock) LinTO instance to configure."""
    linto_cls = stack.enter_context(mock.patch("linto.LinTO"))
    return linto_cls.return_value


class TestAudioExtractionBranching:
    def test_mp4_source_triggers_audio_extraction(self):
        rec = _make_recording(models.RecordingModeChoices.SCREEN_RECORDING)
        assert rec.extension == FileExtension.MP4.value

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"fake mp4 bytes")
            extract_mock = stack.enter_context(
                mock.patch.object(
                    linto_task,
                    "extract_audio_from_video",
                    return_value=b"OggS extracted",
                )
            )
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock(side_effect=RuntimeError("stop"))

            with pytest.raises(RuntimeError, match="stop"):
                linto_task._process_linto_transcription_sync(str(rec.id))

        extract_mock.assert_called_once_with(b"fake mp4 bytes", output_format="copy")
        _, kwargs = sdk.upload.call_args
        assert kwargs["file"] == b"OggS extracted"

    def test_ogg_source_skips_extraction(self):
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)
        assert rec.extension == FileExtension.OGG.value

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            extract_mock = stack.enter_context(
                mock.patch.object(linto_task, "extract_audio_from_video")
            )
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock(side_effect=RuntimeError("stop"))

            with pytest.raises(RuntimeError, match="stop"):
                linto_task._process_linto_transcription_sync(str(rec.id))

        extract_mock.assert_not_called()
        _, kwargs = sdk.upload.call_args
        assert kwargs["file"] == b"OggS raw"


class TestFreshUpload:
    def test_upload_checkpoints_conversation_id(self):
        """A fresh run uploads once and persists conversation_id before polling."""
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock(return_value="new-conv")
            # Halt right after the checkpoint, in the polling step.
            sdk.poll_media = mock.AsyncMock(side_effect=RuntimeError("stop-poll"))

            with pytest.raises(RuntimeError, match="stop-poll"):
                linto_task._process_linto_transcription_sync(str(rec.id))

            sdk.upload.assert_called_once()

        rec.refresh_from_db()
        assert rec.linto_state["conversation_id"] == "new-conv"

    def test_transient_upload_failure_does_not_checkpoint(self):
        """A transient upload failure → TransientError, no conversation_id stored."""
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock(
                side_effect=aiohttp.ClientConnectionError("studio down")
            )

            with pytest.raises(TransientError):
                linto_task._process_linto_transcription_sync(str(rec.id))

        rec.refresh_from_db()
        assert rec.linto_state.get("conversation_id") is None


class TestResume:
    def test_resume_does_not_reupload_and_refetches_media(self, settings):
        """With a checkpointed conversation_id + done transcription, resume must
        NOT call upload and must re-fetch the media via the one-shot get_media."""
        # Twake disabled → on resume neither the upload nor the twake bytes are
        # needed, so the S3 GET is skipped entirely.
        settings.CLOUDERY_URL = None
        settings.CLOUDERY_TOKEN = None
        rec = _make_recording(
            models.RecordingModeChoices.TRANSCRIPT,
            linto_state={
                "conversation_id": "conv-1",
                "steps": {"transcription": True},
                "attempts": 1,
            },
        )

        with contextlib.ExitStack() as stack:
            storage = _patch_storage(stack, b"OggS raw")
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock()
            # get_media is the resume path; halt right after it.
            sdk.get_media = mock.AsyncMock(side_effect=RuntimeError("stop-getmedia"))

            with pytest.raises(RuntimeError, match="stop-getmedia"):
                linto_task._process_linto_transcription_sync(str(rec.id))

            sdk.upload.assert_not_called()
            sdk.get_media.assert_called_once_with("conv-1")
            # No upload and twake not configured → the S3 GET is skipped too.
            storage.connection.meta.client.get_object.assert_not_called()
