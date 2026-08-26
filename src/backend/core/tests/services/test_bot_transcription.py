"""
Test the LinTO live-transcription lifecycle service (browser-first).

The BROWSER drives Studio via the JS SDK; this service only mints the native bot
join token, gates on permissions, drives the banner/egress/summary lifecycle, and
(teardown/summary) talks to Studio under the service account. Studio HTTP is
mocked; LiveKit side effects (banner metadata, native-agent stop, token mint,
egress) are patched.
"""

# pylint: disable=redefined-outer-name,protected-access

from unittest import mock

import pytest
import responses

from core import models
from core.factories import RoomFactory, UserFactory
from core.services.bot_transcription import (
    BotTranscriptionException,
    BotTranscriptionService,
    PermissionDeniedError,
)

pytestmark = pytest.mark.django_db

STUDIO = "http://studio.test"


@pytest.fixture
def studio_settings(settings):
    """Wire the service to fake Studio endpoints with a static service token."""
    settings.LINTO_STUDIO_BASE_URL = STUDIO
    settings.LINTO_STUDIO_API_TOKEN = "static-token"
    settings.LINTO_STUDIO_AUTH_EMAIL = ""
    settings.LINTO_STUDIO_AUTH_PASSWORD = ""
    settings.LINTO_STUDIO_BROWSER_API_URL = "http://studio.browser"
    settings.LINTO_NATIVE_LIVEKIT_URL = "ws://livekit:7880"
    return settings


def _owner_room():
    owner = UserFactory()
    room = RoomFactory()
    room.accesses.create(user=owner, role=models.RoleChoices.OWNER)
    return room, owner


class TestPrepare:
    """prepare: permission gate + native token mint + native-subtitle stop."""

    def test_denied_for_non_admin(self, studio_settings):
        room = RoomFactory()
        user = (
            UserFactory()
        )  # not admin/owner → transcript_permission defaults admin_owner
        with pytest.raises(PermissionDeniedError):
            BotTranscriptionService().prepare(room, "chan-1", user=user)

    def test_mints_token_and_stops_native_subtitles(self, studio_settings):
        room, owner = _owner_room()
        with (
            mock.patch.object(
                BotTranscriptionService, "_stop_native_subtitles"
            ) as stop_native,
            mock.patch(
                "core.services.bot_transcription.generate_bot_join_token",
                return_value="lk-jwt",
            ) as mint,
        ):
            result = BotTranscriptionService().prepare(room, "chan-1", user=owner)
        mint.assert_called_once_with(str(room.id), "chan-1")
        stop_native.assert_called_once_with(room)
        assert result == {
            "token": "lk-jwt",
            "livekit_url": "ws://livekit:7880",
            "room": str(room.id),
        }


class TestMarkStarted:
    """mark_started: persist state + banner; record add-on gated."""

    @pytest.fixture(autouse=True)
    def _no_livekit(self):
        with (
            mock.patch.object(BotTranscriptionService, "_set_banner") as banner,
            mock.patch.object(
                BotTranscriptionService, "_start_recording", return_value="rec-1"
            ) as rec,
        ):
            self.banner = banner
            self.rec = rec
            yield

    def test_denied_for_non_admin(self, studio_settings):
        room = RoomFactory()
        user = UserFactory()
        with pytest.raises(PermissionDeniedError):
            BotTranscriptionService().mark_started(
                room, {"session_id": "s", "channel_id": "c"}, user=user
            )

    def test_persists_state_and_lights_banner(self, studio_settings):
        room, owner = _owner_room()
        data = {
            "session_id": "sess-1",
            "channel_id": "chan-1",
            "org_id": "org-1",
            "bot_id": "bot-1",
            "summary": True,
            "record": False,
        }
        result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["status"] == "running"
        room.refresh_from_db()
        linto = room.configuration["linto"]
        assert linto["session_id"] == "sess-1"
        assert linto["channel_id"] == "chan-1"
        assert linto["bot_id"] == "bot-1"
        assert linto["org_id"] == "org-1"
        assert linto["user_id"] == str(owner.id)
        assert "recording_id" not in linto  # record was False → no egress
        self.banner.assert_called_once_with(room, True, user=owner)
        self.rec.assert_not_called()

    def test_record_add_on_dropped_without_screen_permission(self, studio_settings):
        # transcript authenticated (any logged user), screen_recording admin_owner.
        room = RoomFactory(configuration={"transcript_permission": "authenticated"})
        user = UserFactory()  # logged in but not admin/owner
        result = BotTranscriptionService().mark_started(
            room, {"session_id": "s", "channel_id": "c", "record": True}, user=user
        )
        assert result["record"] is False
        self.rec.assert_not_called()


class TestMarkStopped:
    """mark_stopped: who-can-stop gate, banner off, summary enqueue, state clear."""

    @pytest.fixture(autouse=True)
    def _no_livekit(self):
        with (
            mock.patch.object(BotTranscriptionService, "_set_banner"),
            mock.patch.object(BotTranscriptionService, "_stop_recording"),
            mock.patch.object(BotTranscriptionService, "_enqueue_summary") as enqueue,
        ):
            self.enqueue = enqueue
            yield

    def test_idle_when_no_run(self, studio_settings):
        room = RoomFactory()
        assert BotTranscriptionService().mark_stopped(room, {}, user=None) == {
            "status": "idle"
        }

    def test_denied_for_stranger(self, studio_settings):
        room = RoomFactory()
        room.configuration = {"linto": {"user_id": "999", "session_id": "s"}}
        room.save()
        with pytest.raises(PermissionDeniedError):
            BotTranscriptionService().mark_stopped(room, {}, user=UserFactory())

    def test_starter_stops_and_enqueues_summary(self, studio_settings):
        room = RoomFactory()
        starter = UserFactory()
        room.configuration = {
            "linto": {
                "user_id": str(starter.id),
                "org_id": "org-1",
                "session_id": "sess-1",
                "summary": True,
            }
        }
        room.save()
        result = BotTranscriptionService().mark_stopped(
            room, {"conversation_name": "linto-x"}, user=starter
        )
        assert result == {"status": "stopped"}
        room.refresh_from_db()
        assert "linto" not in room.configuration
        summary = room.configuration["linto_summary"]
        assert summary["conversation_name"] == "linto-x"
        assert summary["session_id"] == "sess-1"
        self.enqueue.assert_called_once_with(room)


class TestTeardown:
    """teardown: service-account DELETEs then the stopped path (no perm gate)."""

    @responses.activate
    def test_deletes_bot_and_session_then_clears(self, studio_settings):
        room = RoomFactory()
        room.configuration = {
            "linto": {
                "org_id": "org-1",
                "session_id": "sess-1",
                "bot_id": "bot-1",
                "summary": False,
            }
        }
        room.save()
        del_bot = responses.delete(
            f"{STUDIO}/api/organizations/org-1/bots/bot-1", json={}, status=200
        )
        del_qm = responses.delete(
            f"{STUDIO}/api/organizations/org-1/quickMeeting/sess-1",
            json={"success": True},
            status=200,
        )
        with (
            mock.patch.object(BotTranscriptionService, "_set_banner"),
            mock.patch.object(BotTranscriptionService, "_enqueue_summary"),
        ):
            BotTranscriptionService().teardown(room)
        assert del_bot.call_count == 1
        assert del_qm.call_count == 1
        room.refresh_from_db()
        assert "linto" not in room.configuration


class TestDevStudioToken:
    def test_returns_service_token_and_browser_base(self, studio_settings):
        result = BotTranscriptionService().dev_studio_token()
        assert result == {"token": "static-token", "base_url": "http://studio.browser"}

    def test_raises_without_credentials(self, studio_settings):
        studio_settings.LINTO_STUDIO_API_TOKEN = None
        with pytest.raises(BotTranscriptionException):
            BotTranscriptionService().dev_studio_token()


class TestResolveConversationId:
    """resolve_conversation_id stays for the summary task (service account)."""

    @responses.activate
    def test_match_by_from_session_id(self, studio_settings):
        responses.get(
            f"{STUDIO}/api/organizations/org-1/conversations",
            json=[
                {"_id": "c-other", "type": {"from_session_id": "zzz"}},
                {"_id": "c-match", "type": {"from_session_id": "sess-1"}},
            ],
            status=200,
        )
        got = BotTranscriptionService().resolve_conversation_id(
            "org-1", "sess-1", "linto-x"
        )
        assert got == "c-match"
