"""Tests for core.tasks.linto: audio extraction, upload and idempotent resume.

The pipeline was split for crash-safe resume: ``upload`` (checkpoints the
conversation_id), ``poll_media`` (wait) and ``get_media`` (one-shot re-fetch on
a resume where the transcription is already done). These tests drive the inner
``_process_linto_transcription_sync`` with the SDK and S3 storage mocked, halting
early at a controlled boundary to assert the branching/idempotency without
running the whole (soft-failing) downstream pipeline.
"""

# pylint: disable=redefined-outer-name,protected-access,no-member

import contextlib
from unittest import mock

from django.core import mail

import aiohttp
import pytest
from asgiref.sync import async_to_sync

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


class TestStudioCredentials:
    """The deferred pipeline authenticates like the live flow: the service
    account (or integration token) first, the static token as the fallback."""

    def test_uses_the_service_login_when_configured(self, settings):
        settings.LINTO_STUDIO_API_TOKEN = "static-prod-token"
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS audio")
            linto_cls = stack.enter_context(mock.patch("linto.LinTO"))
            linto_cls.return_value.upload = mock.AsyncMock(
                side_effect=RuntimeError("stop")
            )
            stack.enter_context(
                mock.patch(
                    "core.services.bot_transcription.BotTranscriptionService._login",
                    return_value="service-token",
                )
            )
            with pytest.raises(RuntimeError, match="stop"):
                linto_task._process_linto_transcription_sync(str(rec.id))
        assert linto_cls.call_args.kwargs["auth_token"] == "service-token"

    def test_falls_back_to_the_static_token_without_credentials(self, settings):
        settings.LINTO_STUDIO_API_TOKEN = "static-token"
        settings.LINTO_STUDIO_AUTH_EMAIL = ""
        settings.LINTO_STUDIO_AUTH_PASSWORD = ""
        settings.LINTO_STUDIO_INTEGRATION_TOKEN = None
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS audio")
            linto_cls = stack.enter_context(mock.patch("linto.LinTO"))
            linto_cls.return_value.upload = mock.AsyncMock(
                side_effect=RuntimeError("stop")
            )
            with pytest.raises(RuntimeError, match="stop"):
                linto_task._process_linto_transcription_sync(str(rec.id))
        assert linto_cls.call_args.kwargs["auth_token"] == "static-token"


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


class TestCatchupConversation:
    """A segmented recording and the late joiners' conversation."""

    @staticmethod
    def _segmented(linto_state=None, mode=models.RecordingModeChoices.TRANSCRIPT):
        rec = _make_recording(mode, linto_state=linto_state)
        rec.options = {"segments": mode == models.RecordingModeChoices.TRANSCRIPT}
        rec.save(update_fields=["options"])
        return rec

    @staticmethod
    def _patch_catchup(stack, **overrides):
        patches = {
            "recording_audio": mock.Mock(return_value=b"TS segments"),
            "hand_over": mock.Mock(return_value=None),
            "drop_conversation": mock.Mock(),
            **overrides,
        }
        for name, value in patches.items():
            stack.enter_context(
                mock.patch.object(linto_task.linto_catchup, name, value)
            )
        stack.enter_context(
            mock.patch.object(
                linto_task, "extract_audio_from_video", return_value=b"OggS file"
            )
        )
        return patches

    def test_the_audio_file_is_written_from_the_segments(self):
        """The audio file is written from the segments, then uploaded."""
        rec = self._segmented()
        with contextlib.ExitStack() as stack:
            storage = _patch_storage(stack, b"OggS file")
            self._patch_catchup(stack)
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock(side_effect=RuntimeError("stop"))

            with pytest.raises(RuntimeError, match="stop"):
                linto_task._process_linto_transcription_sync(str(rec.id))

            put = storage.connection.meta.client.put_object.call_args.kwargs
            assert put["Key"] == rec.key
            assert put["Body"] == b"OggS file"
            assert sdk.upload.call_args.kwargs["file"] == b"OggS file"
            linto_task.extract_audio_from_video.assert_called_once_with(
                b"TS segments", output_format="ogg", input_suffix=".ts"
            )
        rec.refresh_from_db()
        assert rec.linto_state["steps"]["audio_file"] is True

    def test_a_retry_does_not_write_the_file_again(self):
        """Once written, the file is not made again on a retry."""
        rec = self._segmented(linto_state={"steps": {"audio_file": True}})
        with contextlib.ExitStack() as stack:
            storage = _patch_storage(stack, b"OggS file")
            catchup = self._patch_catchup(stack)
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock(side_effect=RuntimeError("stop"))

            with pytest.raises(RuntimeError, match="stop"):
                linto_task._process_linto_transcription_sync(str(rec.id))

            catchup["recording_audio"].assert_not_called()
            storage.connection.meta.client.put_object.assert_not_called()

    def test_the_late_joiners_conversation_becomes_the_transcript(self):
        """The conversation got its last minutes: no upload, it is polled."""
        rec = self._segmented(linto_state={"catchup": {"conversation_id": "conv-c"}})
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS file")
            self._patch_catchup(stack, hand_over=mock.Mock(return_value="conv-c"))
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock()
            sdk.poll_media = mock.AsyncMock(side_effect=RuntimeError("stop-poll"))

            with pytest.raises(RuntimeError, match="stop-poll"):
                linto_task._process_linto_transcription_sync(str(rec.id))

            sdk.upload.assert_not_called()
            sdk.poll_media.assert_called_once_with("conv-c")
        rec.refresh_from_db()
        assert rec.linto_state["conversation_id"] == "conv-c"

    def test_a_hand_over_held_up_is_retried(self):
        """A hand-over held up is retried."""
        rec = self._segmented(linto_state={"catchup": {"conversation_id": "conv-c"}})
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS file")
            self._patch_catchup(
                stack,
                hand_over=mock.Mock(
                    side_effect=linto_task.linto_catchup.CatchupError("busy")
                ),
            )
            _patch_sdk(stack)

            with pytest.raises(TransientError):
                linto_task._process_linto_transcription_sync(str(rec.id))

    def test_next_to_a_video_the_conversation_goes(self):
        """Next to a video, the conversation is deleted and the video uploaded."""
        rec = self._segmented(
            linto_state={"catchup": {"conversation_id": "conv-c"}},
            mode=models.RecordingModeChoices.SCREEN_RECORDING,
        )
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"fake mp4 bytes")
            catchup = self._patch_catchup(stack)
            sdk = _patch_sdk(stack)
            sdk.upload = mock.AsyncMock(side_effect=RuntimeError("stop"))

            with pytest.raises(RuntimeError, match="stop"):
                linto_task._process_linto_transcription_sync(str(rec.id))

            catchup["drop_conversation"].assert_called_once()
            catchup["hand_over"].assert_not_called()
            sdk.upload.assert_called_once()


class TestTwakeInstance:
    """The Drive delivery goes to the instance stored at sign-in, never one
    built from an email sub."""

    def _deliver(self, settings, **owner):
        settings.CLOUDERY_URL = "http://cloudery.test"
        settings.CLOUDERY_TOKEN = "tok"
        settings.TWAKE_DEV_INSTANCE_OVERRIDE = None
        rec = factories.RecordingFactory(mode=models.RecordingModeChoices.TRANSCRIPT)
        user = factories.UserFactory(**{"sub": "owner-sub", **owner})
        factories.UserRecordingAccessFactory(
            recording=rec, user=user, role=models.RoleChoices.OWNER
        )
        # Resume after every step but Twake; the email step is done so the
        # task returns right after the Twake block.
        rec.linto_state = {
            "conversation_id": "conv-1",
            "steps": dict.fromkeys(
                (
                    "transcription",
                    "tag",
                    "move",
                    "summary",
                    "document",
                    "share",
                    "email",
                ),
                True,
            ),
            "attempts": 1,
        }
        rec.save(update_fields=["linto_state"])

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = _patch_sdk(stack)
            sdk.get_media = mock.AsyncMock(return_value=mock.Mock(full_text="hello"))
            token = stack.enter_context(
                mock.patch(
                    "core.services.twake_drive.get_drive_token",
                    new=mock.AsyncMock(return_value="drive-tok"),
                )
            )
            stack.enter_context(
                mock.patch(
                    "core.services.twake_drive.ensure_meeting_directory",
                    new=mock.AsyncMock(return_value="dir-1"),
                )
            )
            stack.enter_context(
                mock.patch("core.services.twake_drive.save_file", new=mock.AsyncMock())
            )
            stack.enter_context(
                mock.patch(
                    "core.services.twake_drive.build_drive_link",
                    return_value="https://drive.test/#/folder/dir-1",
                )
            )
            linto_task._process_linto_transcription_sync(str(rec.id))
        return token

    def test_an_email_sub_uploads_to_the_stored_instance(self, settings):
        """The token is asked for the instance stored at sign-in."""
        token = self._deliver(
            settings, sub="bob@example.com", twake_instance="bob-ws.twake.app"
        )
        assert token.await_args.kwargs["instance"] == "bob-ws.twake.app"

    def test_an_email_sub_without_instance_skips_the_drive(self, settings):
        """No stored instance and an email sub: no instance is guessed."""
        token = self._deliver(settings, sub="bob@example.com", twake_instance=None)
        token.assert_not_called()


class TestSpeakerIdentification:
    """Org-level speaker identification is opt-in and always soft-fails."""

    def _run_until_poll(self, stack, rec, collection):
        """Wire the SDK so the run halts in poll_media, right after upload."""
        sdk = _patch_sdk(stack)
        sdk.get_org_voiceprint_collection_id = mock.AsyncMock(**collection)
        sdk.upload = mock.AsyncMock(return_value="new-conv")
        sdk.poll_media = mock.AsyncMock(side_effect=RuntimeError("stop-poll"))
        with pytest.raises(RuntimeError, match="stop-poll"):
            linto_task._process_linto_transcription_sync(str(rec.id))
        return sdk

    def test_disabled_by_default_skips_resolution(self):
        """Default (setting off): the org collection is never resolved."""
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = self._run_until_poll(stack, rec, {"return_value": "col-org"})

        sdk.get_org_voiceprint_collection_id.assert_not_called()
        _, kwargs = sdk.upload.call_args
        assert kwargs["speaker_collection_ids"] is None

    def test_enabled_passes_org_collection(self, settings):
        """Setting on + collection resolved: upload carries the collection id."""
        settings.LINTO_SPEAKER_IDENTIFICATION_ENABLED = True
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = self._run_until_poll(stack, rec, {"return_value": "col-org"})

        sdk.get_org_voiceprint_collection_id.assert_awaited_once()
        _, kwargs = sdk.upload.call_args
        assert kwargs["speaker_collection_ids"] == ["col-org"]

    def test_enabled_but_no_collection_uploads_without_it(self, settings):
        """Setting on but org has no collection: fall back to plain diarization."""
        settings.LINTO_SPEAKER_IDENTIFICATION_ENABLED = True
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = self._run_until_poll(stack, rec, {"return_value": None})

        _, kwargs = sdk.upload.call_args
        assert kwargs["speaker_collection_ids"] is None

    def test_resolution_error_soft_fails(self, settings):
        """A resolution error (e.g. missing permission) must not abort upload."""
        settings.LINTO_SPEAKER_IDENTIFICATION_ENABLED = True
        rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)

        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = self._run_until_poll(
                stack, rec, {"side_effect": RuntimeError("no permission")}
            )

        sdk.upload.assert_called_once()
        _, kwargs = sdk.upload.call_args
        assert kwargs["speaker_collection_ids"] is None


class TestSummaryCapability:
    """The deferred pipeline summarizes only for a starter holding the
    `summary` capability, read when the task reaches the summary step."""

    TWAKE_PLAN = {"transcription": {"live": True, "async": True}, "recording": True}

    def _run_until_document(self, settings, options=None):
        """Resume a transcribed recording and halt right after the summary
        step; return the summarize and document mocks and the recording."""
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
        if options is not None:
            rec.options = options
            rec.save(update_fields=["options"])
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = _patch_sdk(stack)
            sdk.get_media = mock.AsyncMock(return_value=mock.Mock())
            stack.enter_context(
                mock.patch.object(linto_task, "_tag_and_move", mock.AsyncMock())
            )
            summarize = stack.enter_context(
                mock.patch.object(
                    linto_task,
                    "_summarize_conversation",
                    mock.AsyncMock(return_value={"summary": "done"}),
                )
            )
            document = stack.enter_context(
                mock.patch.object(
                    linto_task,
                    "_generate_document",
                    mock.AsyncMock(side_effect=RuntimeError("stop-doc")),
                )
            )
            with pytest.raises(RuntimeError, match="stop-doc"):
                linto_task._process_linto_transcription_sync(str(rec.id))
        return summarize, document, rec

    def test_no_summary_without_the_capability(self, settings):
        settings.LINTO_STUDIO_ENABLED = True
        settings.LINTO_ENTITLEMENTS_ENABLED = True
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities",
            return_value=self.TWAKE_PLAN,
        ) as capabilities:
            summarize, document, rec = self._run_until_document(
                settings, options={"summary": True}
            )
        summarize.assert_not_called()
        assert document.call_args.args[2] == {}
        # The capability is the starter's.
        starter = rec.accesses.order_by("created_at").first().user
        assert capabilities.call_args.args[0] == starter

    def test_summary_with_the_capability(self, settings):
        settings.LINTO_STUDIO_ENABLED = True
        settings.LINTO_ENTITLEMENTS_ENABLED = True
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities",
            return_value={**self.TWAKE_PLAN, "summary": True},
        ):
            summarize, document, _rec = self._run_until_document(
                settings, options={"summary_service": "minutes"}
            )
        summarize.assert_awaited_once()
        assert summarize.call_args.kwargs["service_route"] == "minutes"
        assert document.call_args.args[2] == {"summary": "done"}

    def test_summary_turned_off_in_the_panel_stays_off(self, settings):
        settings.LINTO_ENTITLEMENTS_ENABLED = True
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities",
            return_value={"summary": True},
        ):
            summarize, _document, _rec = self._run_until_document(
                settings, options={"summary": False}
            )
        summarize.assert_not_called()

    def test_kill_switch_off_summarizes_as_before(self, settings):
        """LINTO_ENTITLEMENTS_ENABLED=False: no lookup, summary by default."""
        settings.LINTO_ENTITLEMENTS_ENABLED = False
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities"
        ) as capabilities:
            summarize, _document, _rec = self._run_until_document(settings)
        capabilities.assert_not_called()
        summarize.assert_awaited_once()


class TestDelivery:
    """The room owner receives the delivery; the attendees get the summary
    when the starter shares it."""

    SUMMARY = {"success": True, "job_id": "job-1", "content": "## Decisions"}

    def _deliver(self, settings, options):
        settings.CLOUDERY_URL = None
        settings.CLOUDERY_TOKEN = None
        settings.LINTO_ENTITLEMENTS_ENABLED = False
        owner = factories.UserFactory(email="owner@example.com", language="en-us")
        admin = factories.UserFactory(email="admin@example.com")
        starter = factories.UserFactory(email="starter@example.com")
        guest = factories.UserFactory(email="guest@example.com", language="en-us")
        room = factories.RoomFactory(
            users=[(owner, models.RoleChoices.OWNER), (admin, models.RoleChoices.ADMIN)]
        )
        rec = factories.RecordingFactory(
            room=room, mode=models.RecordingModeChoices.TRANSCRIPT, options=options
        )
        rec.grant_initial_accesses(starter=starter)
        rec.linto_state = {
            "conversation_id": "conv-1",
            "steps": {"transcription": True},
            "attempts": 1,
            "attendees": [str(starter.id), str(guest.id), str(owner.id)],
        }
        rec.save(update_fields=["linto_state"])
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = _patch_sdk(stack)
            sdk.get_media = mock.AsyncMock(return_value=mock.Mock(full_text="hi"))
            sdk.share_conversation = mock.AsyncMock()
            stack.enter_context(
                mock.patch.object(linto_task, "_tag_and_move", mock.AsyncMock())
            )
            stack.enter_context(
                mock.patch.object(
                    linto_task,
                    "_summarize_conversation",
                    mock.AsyncMock(return_value=self.SUMMARY),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    linto_task,
                    "_generate_document",
                    mock.AsyncMock(return_value=(b"DOCX", "docx")),
                )
            )
            linto_task._process_linto_transcription_sync(str(rec.id))
        return sdk, rec

    def test_the_room_owner_receives_it_not_the_starter(self, settings):
        """Studio share and email go to the room owner, the document attached."""
        sdk, _rec = self._deliver(settings, {})
        assert sdk.share_conversation.call_args.kwargs["email"] == "owner@example.com"
        assert [m.to for m in mail.outbox] == [["owner@example.com"]]
        assert mail.outbox[0].attachments[0][1] == b"DOCX"

    def test_a_shared_summary_reaches_the_attendees(self, settings):
        """The attendees get the summary document, the owner their recap."""
        _sdk, rec = self._deliver(settings, {"share_summary": True})
        recipients = sorted(m.to[0] for m in mail.outbox)
        assert recipients == [
            "guest@example.com",
            "owner@example.com",
            "starter@example.com",
        ]
        shared = [m for m in mail.outbox if m.to != ["owner@example.com"]]
        assert all(m.attachments[0][1] == b"DOCX" for m in shared)
        assert all("Decisions" not in m.body for m in shared)
        rec.refresh_from_db()
        assert len(rec.linto_state["steps"]["shared"]) == 2


class TestNoSpeech:
    """A recording without speech: no transcript nor summary, the recording goes
    to Twake Drive and the owner gets a no-speech email, not a failure."""

    NO_SPEECH_JOB = {
        "jobs": {
            "transcription": {
                "state": "error",
                "error": {"code": "NO_SPEECH", "status": 422},
            }
        }
    }

    def _run(self, settings, *, linto_state, poll=None, status=None):
        settings.CLOUDERY_URL = "https://cloudery.example.com"
        settings.CLOUDERY_TOKEN = "cloudery-token"
        owner = factories.UserFactory(email="owner@example.com", language="en-us")
        room = factories.RoomFactory(users=[(owner, models.RoleChoices.OWNER)])
        rec = factories.RecordingFactory(
            room=room, mode=models.RecordingModeChoices.TRANSCRIPT
        )
        rec.linto_state = linto_state
        rec.save(update_fields=["linto_state"])
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = _patch_sdk(stack)
            sdk.poll_media = mock.AsyncMock()
            sdk.api_service.get_media_status = mock.AsyncMock(return_value=status)
            stack.enter_context(
                mock.patch.object(
                    linto_task, "_await_media", mock.AsyncMock(return_value=poll)
                )
            )
            twake = stack.enter_context(
                mock.patch.object(
                    linto_task,
                    "_deliver_to_twake",
                    mock.AsyncMock(return_value="https://drive.example.com/#/d1"),
                )
            )
            summarize = stack.enter_context(
                mock.patch.object(linto_task, "_summarize_conversation")
            )
            release = stack.enter_context(
                mock.patch.object(linto_task, "_release_segments", mock.AsyncMock())
            )
            linto_task._process_linto_transcription_sync(str(rec.id))
        summarize.assert_not_called()
        release.assert_awaited_once()
        rec.refresh_from_db()
        return rec, sdk, twake

    def _assert_no_speech_delivery(self, rec, twake):
        kwargs = twake.call_args.kwargs
        assert kwargs["transcript_text"] is None
        assert kwargs["conversation_id"] is None
        assert kwargs["pdf_content"] is None
        files = async_to_sync(kwargs["extra_files_provider"])()
        assert [(f[1], f[2]) for f in files] == [(b"OggS raw", "audio/ogg")]
        assert [m.to for m in mail.outbox] == [["owner@example.com"]]
        message = mail.outbox[0]
        assert message.subject == "No speech was detected in your meeting"
        for text in (message.body, message.alternatives[0][0]):
            assert "No speech was detected in the recording of" in text
            assert "drive.example.com" in text
        assert rec.linto_state["no_speech"] is True
        assert rec.linto_state["steps"]["email"] is True
        assert "transcription" not in rec.linto_state["steps"]
        assert rec.status != models.RecordingStatusChoices.NOTIFICATION_FAILED

    def test_a_no_speech_error_delivers_the_recording(self, settings):
        """Studio's NO_SPEECH code: the recording and a no-speech email."""
        rec, sdk, twake = self._run(
            settings,
            linto_state={"conversation_id": "conv-1", "attempts": 0},
            poll={"success": False},
            status=self.NO_SPEECH_JOB,
        )
        sdk.api_service.get_media_status.assert_awaited_once_with(mediaId="conv-1")
        self._assert_no_speech_delivery(rec, twake)

    def test_an_empty_conversation_is_no_speech(self, settings):
        """The late joiners' conversation ends done without any turn."""
        empty = mock.Mock(turns=[], full_text="")
        rec, sdk, twake = self._run(
            settings,
            linto_state={"conversation_id": "conv-1", "attempts": 0},
            poll={"success": True, "media": empty},
        )
        sdk.api_service.get_media_status.assert_not_called()
        self._assert_no_speech_delivery(rec, twake)

    def test_a_resume_does_not_poll_again(self, settings):
        """A no-speech outcome checkpointed: the delivery resumes, no polling."""
        rec, sdk, twake = self._run(
            settings,
            linto_state={"conversation_id": "conv-1", "no_speech": True, "steps": {}},
        )
        sdk.poll_media.assert_not_called()
        self._assert_no_speech_delivery(rec, twake)

    def test_another_transcription_error_still_fails(self):
        """An error without the NO_SPEECH code stays a pipeline failure."""
        rec = _make_recording(
            models.RecordingModeChoices.TRANSCRIPT,
            linto_state={"conversation_id": "conv-1", "attempts": 0},
        )
        with contextlib.ExitStack() as stack:
            _patch_storage(stack, b"OggS raw")
            sdk = _patch_sdk(stack)
            sdk.poll_media = mock.AsyncMock()
            sdk.api_service.get_media_status = mock.AsyncMock(
                return_value={"jobs": {"transcription": {"state": "error"}}}
            )
            stack.enter_context(
                mock.patch.object(
                    linto_task,
                    "_await_media",
                    mock.AsyncMock(return_value={"success": False}),
                )
            )
            with pytest.raises(RuntimeError, match="transcription failed"):
                linto_task._process_linto_transcription_sync(str(rec.id))
        rec.refresh_from_db()
        assert "no_speech" not in rec.linto_state
        assert not mail.outbox
