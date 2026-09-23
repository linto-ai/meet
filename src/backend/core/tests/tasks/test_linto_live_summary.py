"""Tests for ``core.tasks.linto.process_bot_live_summary`` (bot live summary).

The task is driven through its inner ``_process_bot_live_summary_sync`` with
the LinTO SDK (``linto.LinTO``) and the ``BotTranscriptionService`` (Studio
HTTP) mocked, so nothing hits the network. Emails go to Django's in-memory
outbox. The shared delivery helpers are exercised for real (checkpointing in
``room.configuration["linto_summary"]["steps"]``).
"""

# pylint: disable=redefined-outer-name,protected-access,unused-argument,no-member

import contextlib
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from django.core import mail

import aiohttp
import pytest

from core import factories, models
from core.services.twake_drive import (
    build_linto_shortcut_filename,
    build_recording_filename,
    build_summary_filename,
    build_transcript_filename,
)
from core.tasks import linto as linto_task
from core.tasks._errors import TransientError

pytestmark = pytest.mark.django_db

PAYLOAD = {
    "org_id": "org-1",
    "session_id": "sess-1",
    "conversation_name": "linto-room-123",
    "recipient_user_id": None,
    "record": False,
    "steps": {},
    "attempts": 0,
}


def _make_room(payload=None, *, recipient=None, owner=None):
    """Room with a pending ``linto_summary`` payload (and optional users)."""
    payload = {**(PAYLOAD if payload is None else payload)}
    if recipient is not None:
        payload["recipient_user_id"] = str(recipient.id)
    room = factories.RoomFactory(configuration={"linto_summary": payload})
    if owner is not None:
        factories.UserResourceAccessFactory(
            resource=room, user=owner, role=models.RoleChoices.OWNER
        )
    return room


@pytest.fixture
def no_twake(settings):
    """Twake Drive disabled (the default in most tests)."""
    settings.CLOUDERY_URL = None
    settings.CLOUDERY_TOKEN = None
    settings.LINTO_STUDIO_BASE_URL = "http://studio.test"
    settings.LINTO_LLM_SUMMARY_ENABLED = True
    settings.LINTO_PUBLICATION_FORMAT = "pdf"
    return settings


@pytest.fixture
def fast_resolve():
    """No sleep between conversation-resolution re-queries."""
    with mock.patch.object(linto_task, "_CONV_RESOLVE_DELAY", 0):
        yield


class _Media:
    """Minimal stand-in for the SDK media object."""

    def __init__(self, full_text="hello world", turns=None):
        self.full_text = full_text
        self.turns = turns or []


def _patch_sdk(stack, *, summary=None, services=None):
    """Patch ``linto.LinTO``; return the mock instance wired for a happy run.

    ``summary=None`` → no LLM service (document = raw DOCX download).
    ``summary={"content": ..., "jobId": ...}`` → the summarize handle fires
    ``done`` immediately with that payload (document = template export).
    ``services`` → what ``list_llm_services`` answers (default one "llm" route).
    """
    sdk = stack.enter_context(mock.patch("linto.LinTO")).return_value
    sdk.get_media = mock.AsyncMock(return_value=_Media())
    sdk.ensure_tag = mock.AsyncMock(return_value="tag-1")
    sdk.add_conversation_tag = mock.AsyncMock()
    sdk.ensure_folder = mock.AsyncMock(return_value="folder-1")
    sdk.move_to_folder = mock.AsyncMock()
    sdk.download_conversation = mock.AsyncMock(return_value=b"DOCX")
    sdk.share_conversation = mock.AsyncMock()
    if summary is None:
        sdk.list_llm_services = mock.AsyncMock(return_value=[])
    else:
        sdk.list_llm_services = mock.AsyncMock(
            return_value=services if services is not None else [{"route": "llm"}]
        )
        handle = mock.Mock()
        callbacks = {}

        def _on(event, callback):
            callbacks[event] = callback
            if event == "update":  # last registration → fire completion
                callbacks["done"](summary)

        handle.on.side_effect = _on
        sdk.summarize = mock.AsyncMock(return_value=handle)
        sdk.get_publication_templates = mock.AsyncMock(
            return_value=[{"_id": "tpl-1", "is_default": True}]
        )
        sdk.export_with_template = mock.AsyncMock(return_value=b"%PDF")
    return sdk


def _patch_service(stack, *, conversation_id="conv-1", owner=None):
    """Patch ``BotTranscriptionService``; return the mock service instance."""
    service_cls = stack.enter_context(
        mock.patch("core.services.bot_transcription.BotTranscriptionService")
    )
    service = service_cls.return_value
    service._login.return_value = "fresh-token"
    service.resolve_conversation_id.return_value = conversation_id
    service._room_owner.return_value = owner
    return service


def _spy_checkpoints(stack):
    """Record the step names marked done, in order."""
    marked = []
    original = linto_task._StepCheckpoint.mark

    async def _mark(self, name):
        marked.append(name)
        await original(self, name)

    stack.enter_context(mock.patch.object(linto_task._StepCheckpoint, "mark", _mark))
    return marked


class TestNoOps:
    """Cases where the task must exit quietly without touching Studio."""

    def test_room_gone_is_noop(self, no_twake):
        """A deleted room → nothing to do, no Studio/SDK access."""
        with contextlib.ExitStack() as stack:
            sdk_cls = stack.enter_context(mock.patch("linto.LinTO"))
            service_cls = stack.enter_context(
                mock.patch("core.services.bot_transcription.BotTranscriptionService")
            )
            linto_task._process_bot_live_summary_sync(
                "00000000-0000-0000-0000-000000000000"
            )
        sdk_cls.assert_not_called()
        service_cls.assert_not_called()

    def test_missing_payload_is_noop(self, no_twake):
        """No pending ``linto_summary`` → nothing to do, config untouched."""
        room = factories.RoomFactory(configuration={"other": True})
        with contextlib.ExitStack() as stack:
            sdk_cls = stack.enter_context(mock.patch("linto.LinTO"))
            service_cls = stack.enter_context(
                mock.patch("core.services.bot_transcription.BotTranscriptionService")
            )
            linto_task._process_bot_live_summary_sync(str(room.id))
        sdk_cls.assert_not_called()
        service_cls.assert_not_called()
        room.refresh_from_db()
        assert room.configuration == {"other": True}

    def test_incomplete_payload_is_cleared(self, no_twake):
        """A payload missing org/session/name is dropped without retry."""
        room = _make_room({**PAYLOAD, "session_id": None})
        with contextlib.ExitStack() as stack:
            sdk_cls = stack.enter_context(mock.patch("linto.LinTO"))
            _patch_service(stack)
            linto_task._process_bot_live_summary_sync(str(room.id))
        sdk_cls.assert_not_called()
        room.refresh_from_db()
        assert "linto_summary" not in room.configuration


class TestConversationResolution:
    """The finalized Studio conversation must be found before anything else."""

    def test_not_found_raises_transient(self, no_twake, fast_resolve):
        """Conversation never found → TransientError (Celery retry), marker kept."""
        room = _make_room()
        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(stack)
            service = _patch_service(stack, conversation_id=None)
            with pytest.raises(TransientError):
                linto_task._process_bot_live_summary_sync(str(room.id))

        assert (
            service.resolve_conversation_id.call_count
            == linto_task._CONV_RESOLVE_ATTEMPTS
        )
        service.resolve_conversation_id.assert_called_with(
            "org-1", "sess-1", "linto-room-123"
        )
        sdk.get_media.assert_not_called()
        room.refresh_from_db()
        payload = room.configuration["linto_summary"]
        assert payload["attempts"] == 1
        assert payload.get("conversation_id") is None

    def test_found_after_retry_is_checkpointed(self, no_twake, fast_resolve):
        """Found on a later in-attempt query → id persisted in the payload."""
        room = _make_room()
        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(stack)
            service = _patch_service(stack)
            service.resolve_conversation_id.side_effect = [None, None, "conv-9"]
            # Halt right after the checkpoint (first SDK call).
            sdk.get_media = mock.AsyncMock(side_effect=RuntimeError("stop"))
            with pytest.raises(RuntimeError, match="stop"):
                linto_task._process_bot_live_summary_sync(str(room.id))

        assert service.resolve_conversation_id.call_count == 3
        room.refresh_from_db()
        assert room.configuration["linto_summary"]["conversation_id"] == "conv-9"

    def test_transient_media_fetch_is_retried(self, no_twake, fast_resolve):
        """A network failure on get_media is transient; the marker is kept."""
        room = _make_room()
        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(stack)
            _patch_service(stack)
            sdk.get_media = mock.AsyncMock(
                side_effect=aiohttp.ClientConnectionError("studio down")
            )
            with pytest.raises(TransientError):
                linto_task._process_bot_live_summary_sync(str(room.id))

        room.refresh_from_db()
        # Still pending: the retry resumes with the resolved conversation.
        assert room.configuration["linto_summary"]["conversation_id"] == "conv-1"


class TestHappyPath:
    """Full delivery with a recipient: helpers run in order, steps checkpointed."""

    def test_delivers_and_clears_marker(self, no_twake, fast_resolve):
        """Happy path: SDK calls in order, steps checkpointed, email sent, marker gone."""
        recipient = factories.UserFactory(email="starter@example.com")
        room = _make_room(recipient=recipient)

        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(stack)
            service = _patch_service(stack)
            marked = _spy_checkpoints(stack)
            linto_task._process_bot_live_summary_sync(str(room.id))

        service._login.assert_called_once()
        sdk.get_media.assert_awaited_once_with("conv-1")

        # Delivery helpers ran in pipeline order against the SDK.
        sdk_calls = [name for name, _args, _kwargs in sdk.mock_calls]
        expected = [
            "get_media",
            "ensure_tag",
            "add_conversation_tag",
            "ensure_folder",
            "move_to_folder",
            "list_llm_services",
            "download_conversation",
            "share_conversation",
        ]
        assert [c for c in sdk_calls if c in expected] == expected
        sdk.share_conversation.assert_awaited_once_with(
            conversation_id="conv-1",
            email="starter@example.com",
            right=31,
            notify=False,
        )
        # The room owner is never consulted when the starter is known.
        service._room_owner.assert_not_called()

        # Steps were checkpointed as they completed (no summary service → no
        # "summary" step; Twake disabled → no "twake" step).
        assert marked == ["tag", "move", "document", "share", "email"]

        # Recap email with the DOCX attached (no Twake link).
        assert len(mail.outbox) == 1
        email = mail.outbox[0]
        assert email.to == ["starter@example.com"]
        assert len(email.attachments) == 1
        filename, content, mimetype = email.attachments[0]
        assert filename.endswith(".docx")
        assert content == b"DOCX"
        assert "wordprocessingml" in mimetype

        # Pending marker removed once delivery completed.
        room.refresh_from_db()
        assert "linto_summary" not in room.configuration

    def test_summary_export_path(self, no_twake, fast_resolve):
        """With an LLM service the document is the template export + preview."""
        recipient = factories.UserFactory(email="starter@example.com")
        room = _make_room(recipient=recipient)

        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(
                stack, summary={"content": "## Key points\n- one", "jobId": "job-1"}
            )
            _patch_service(stack)
            marked = _spy_checkpoints(stack)
            linto_task._process_bot_live_summary_sync(str(room.id))

        sdk.summarize.assert_awaited_once_with("conv-1", "llm")
        sdk.export_with_template.assert_awaited_once_with(
            job_id="job-1", format="pdf", template_id="tpl-1", conversation_id="conv-1"
        )
        sdk.download_conversation.assert_not_called()
        assert marked == ["tag", "move", "summary", "document", "share", "email"]

        assert len(mail.outbox) == 1
        email = mail.outbox[0]
        assert "Key points" in email.body
        filename, content, mimetype = email.attachments[0]
        assert filename.endswith(".pdf")
        assert content == b"%PDF"
        assert mimetype == "application/pdf"

    def test_summarizes_with_the_service_chosen_in_the_panel(
        self, no_twake, fast_resolve
    ):
        recipient = factories.UserFactory(email="starter@example.com")
        room = _make_room(
            {**PAYLOAD, "summary_service": "actions"}, recipient=recipient
        )
        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(
                stack,
                summary={"content": "## Actions", "jobId": "job-1"},
                services=[{"route": "llm"}, {"route": "actions"}],
            )
            _patch_service(stack)
            linto_task._process_bot_live_summary_sync(str(room.id))
        sdk.summarize.assert_awaited_once_with("conv-1", "actions")

    def test_falls_back_when_the_chosen_service_is_gone(
        self, no_twake, fast_resolve, settings
    ):
        settings.LINTO_LLM_SERVICE_ROUTE = None
        recipient = factories.UserFactory(email="starter@example.com")
        room = _make_room(
            {**PAYLOAD, "summary_service": "vanished"}, recipient=recipient
        )
        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(
                stack, summary={"content": "## Key points", "jobId": "job-1"}
            )
            _patch_service(stack)
            linto_task._process_bot_live_summary_sync(str(room.id))
        sdk.summarize.assert_awaited_once_with("conv-1", "llm")

    def test_falls_back_to_room_owner(self, no_twake, fast_resolve):
        """Unknown recipient_user_id → the room owner receives the summary."""
        owner = factories.UserFactory(email="owner@example.com")
        room = _make_room(
            {**PAYLOAD, "recipient_user_id": "00000000-0000-0000-0000-0000000000ff"},
            owner=owner,
        )

        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(stack)
            service = _patch_service(stack, owner=owner)
            linto_task._process_bot_live_summary_sync(str(room.id))

        service._room_owner.assert_called_once()
        sdk.share_conversation.assert_awaited_once()
        assert sdk.share_conversation.call_args.kwargs["email"] == "owner@example.com"
        assert [m.to for m in mail.outbox] == [["owner@example.com"]]

    def test_resume_skips_done_steps(self, no_twake, fast_resolve):
        """A retry with everything checkpointed only re-fetches the media."""
        recipient = factories.UserFactory(email="starter@example.com")
        room = _make_room(
            {
                **PAYLOAD,
                "conversation_id": "conv-1",
                "attempts": 1,
                "steps": {
                    "tag": True,
                    "move": True,
                    "summary": True,
                    "document": True,
                    "share": True,
                    "email": True,
                },
            },
            recipient=recipient,
        )

        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(stack)
            service = _patch_service(stack)
            linto_task._process_bot_live_summary_sync(str(room.id))

        service.resolve_conversation_id.assert_not_called()
        sdk.get_media.assert_awaited_once_with("conv-1")
        sdk.ensure_tag.assert_not_called()
        sdk.list_llm_services.assert_not_called()
        sdk.download_conversation.assert_not_called()
        sdk.share_conversation.assert_not_called()
        assert mail.outbox == []
        room.refresh_from_db()
        assert "linto_summary" not in room.configuration


class TestNoRecipient:
    """No starter and no room owner: share/Twake/email skipped, no error."""

    def test_graceful_without_recipient(self, no_twake, fast_resolve):
        """Studio-side steps still run; recipient-bound ones are skipped, no error."""
        no_twake.CLOUDERY_URL = "http://cloudery.test"
        no_twake.CLOUDERY_TOKEN = "tok"
        room = _make_room()

        with contextlib.ExitStack() as stack:
            sdk = _patch_sdk(stack)
            _patch_service(stack, owner=None)
            get_token = stack.enter_context(
                mock.patch("core.services.twake_drive.get_drive_token")
            )
            marked = _spy_checkpoints(stack)
            linto_task._process_bot_live_summary_sync(str(room.id))

        # Studio-side steps still ran; recipient-bound ones were skipped.
        sdk.add_conversation_tag.assert_awaited_once()
        sdk.download_conversation.assert_awaited_once()
        sdk.share_conversation.assert_not_called()
        get_token.assert_not_called()
        assert mail.outbox == []
        # "email" is checkpointed (nothing to send → no failure).
        assert marked == ["tag", "move", "document", "email"]
        room.refresh_from_db()
        assert "linto_summary" not in room.configuration


class TestTwakeTextOnly:
    """The live path pushes transcript/summary/document but NO media bytes."""

    def test_twake_upload_is_text_only(self, no_twake, fast_resolve):
        """Transcript/summary/shortcut/document are pushed, never Enregistrement_*."""
        no_twake.CLOUDERY_URL = "http://cloudery.test"
        no_twake.CLOUDERY_TOKEN = "tok"
        no_twake.LINTO_STUDIO_FRONTEND_URL = "http://studio-front.test"
        recipient = factories.UserFactory(
            sub="starter-sub", email="starter@example.com"
        )
        stopped_at = "2026-09-23T05:53:57+00:00"
        room = _make_room({**PAYLOAD, "stopped_at": stopped_at}, recipient=recipient)

        with contextlib.ExitStack() as stack:
            _patch_sdk(stack)
            _patch_service(stack)
            stack.enter_context(
                mock.patch(
                    "core.services.twake_drive.get_drive_token",
                    mock.AsyncMock(return_value="drive-token"),
                )
            )
            stack.enter_context(
                mock.patch(
                    "core.services.twake_drive.ensure_meeting_directory",
                    mock.AsyncMock(return_value="dir-1"),
                )
            )
            save_file = stack.enter_context(
                mock.patch("core.services.twake_drive.save_file", mock.AsyncMock())
            )
            stack.enter_context(
                mock.patch(
                    "core.services.twake_drive.build_drive_link",
                    return_value="http://drive.test/dir-1",
                )
            )
            marked = _spy_checkpoints(stack)
            linto_task._process_bot_live_summary_sync(str(room.id))

        # Names follow the Twake Drive convention in the recipient's language
        # (core.services.twake_drive builders): the transcript note, the LinTO
        # shortcut and the published document — no recording bytes, no summary
        # note.
        meeting = SimpleNamespace(
            created_at=datetime.fromisoformat(stopped_at), room_id=room.id
        )
        lang = recipient.language
        filenames = [c.kwargs["filename"] for c in save_file.await_args_list]
        assert build_transcript_filename(meeting, lang) in filenames
        assert build_linto_shortcut_filename(lang) in filenames
        assert (
            build_summary_filename(meeting, "docx", lang) in filenames
        )  # LINTO_PUBLICATION_FORMAT default
        assert build_recording_filename(meeting, "mp4", lang) not in filenames
        assert build_recording_filename(meeting, "ogg", lang) not in filenames
        assert not any(f.endswith((".mp4", ".ogg")) for f in filenames)
        assert len(filenames) == 3
        assert marked == ["tag", "move", "document", "share", "twake", "email"]

        # With a drive link the email carries the link instead of the attachment.
        assert len(mail.outbox) == 1
        assert mail.outbox[0].attachments == []
        assert "http://drive.test/dir-1" in mail.outbox[0].body
        room.refresh_from_db()
        assert "linto_summary" not in room.configuration
