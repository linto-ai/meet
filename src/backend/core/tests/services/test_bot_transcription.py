"""
Test the LinTO transcription-bot service (BotTranscriptionService).

The service talks to LinTO Studio and Session-API over HTTP via ``requests``;
every outbound call is mocked (``responses`` / ``unittest.mock``) so the tests
NEVER hit the network.
"""

# pylint: disable=redefined-outer-name,protected-access,too-many-public-methods

import json
from unittest import mock

import pytest
import responses

from core import models
from core.factories import RoomFactory, UserFactory
from core.services.bot_transcription import (
    BotTranscriptionException,
    BotTranscriptionService,
    NoQuickMeetingProfile,
)

pytestmark = pytest.mark.django_db

STUDIO = "http://studio.test"
SESSION_API = "http://session.test"


@pytest.fixture
def studio_settings(settings):
    """Wire the service to fake Studio/Session-API endpoints with a static token."""
    settings.LINTO_STUDIO_BASE_URL = STUDIO
    settings.LINTO_SESSION_API_URL = SESSION_API
    settings.LINTO_STUDIO_API_TOKEN = "static-token"
    settings.LINTO_STUDIO_AUTH_EMAIL = ""
    settings.LINTO_STUDIO_AUTH_PASSWORD = ""
    settings.LINTO_STUDIO_DEFAULT_ORG_ID = ""
    settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = ""
    settings.LINTO_VISIO_NATIVE_ENABLED = False
    settings.LINTO_BOT_PROVIDER = "visio"
    settings.MEET_PUBLIC_URL = "http://meet.test"
    return settings


class TestAuthAndHeaders:
    """_login / _headers: static-token fallback, login flow, and no-credentials error."""

    def test_headers_use_static_token_without_network(self, studio_settings):
        """With only a static API token set, _headers must NOT call the network."""
        service = BotTranscriptionService()
        # responses is not active here → any HTTP call would raise ConnectionError.
        assert service._headers() == {"Authorization": "Bearer static-token"}

    @responses.activate
    def test_login_prefers_credentials(self, studio_settings):
        """When email/password are set, _login authenticates and uses auth_token."""
        studio_settings.LINTO_STUDIO_AUTH_EMAIL = "svc@test"
        studio_settings.LINTO_STUDIO_AUTH_PASSWORD = "pw"
        responses.post(
            f"{STUDIO}/auth/login", json={"auth_token": "live-token"}, status=200
        )
        service = BotTranscriptionService()
        assert service._headers() == {"Authorization": "Bearer live-token"}

    def test_login_without_any_credential_raises(self, studio_settings):
        """No email/password and no static token → BotTranscriptionException."""
        studio_settings.LINTO_STUDIO_API_TOKEN = None
        service = BotTranscriptionService()
        with pytest.raises(BotTranscriptionException):
            service._login()


class TestResolveOrg:
    """_resolve_org: configured default, dynamic discovery (Visio-preferred), empty."""

    def test_prefers_configured_default(self, studio_settings):
        """A configured LINTO_STUDIO_DEFAULT_ORG_ID short-circuits discovery."""
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org-default"
        service = BotTranscriptionService()
        assert service._resolve_org({}) == "org-default"

    @responses.activate
    def test_prefers_visio_org(self, studio_settings):
        """Among discovered orgs, a "Visio" org wins over the first one."""
        responses.get(
            f"{STUDIO}/api/organizations/",
            json=[
                {"_id": "org-a", "name": "Other"},
                {"_id": "org-visio", "name": "My Visio Org"},
            ],
            status=200,
        )
        service = BotTranscriptionService()
        assert service._resolve_org({}) == "org-visio"

    @responses.activate
    def test_no_org_raises(self, studio_settings):
        """No organization available → BotTranscriptionException."""
        responses.get(f"{STUDIO}/api/organizations/", json=[], status=200)
        service = BotTranscriptionService()
        with pytest.raises(BotTranscriptionException):
            service._resolve_org({})


class TestResolveProfile:
    """_resolve_profile: explicit id, configured default, discovery, empty → NoQuickMeetingProfile."""

    def test_explicit_profile_from_config(self, studio_settings):
        """asr_profile_id in the config wins with no HTTP call."""
        service = BotTranscriptionService()
        assert service._resolve_profile({}, "org", {"asr_profile_id": "p-1"}) == "p-1"

    @responses.activate
    def test_first_discovered_profile(self, studio_settings):
        """With no explicit/default id, the first quickMeeting profile is used."""
        responses.get(
            f"{STUDIO}/api/organizations/org/transcriber_profiles",
            json=[{"id": "p-first"}, {"id": "p-second"}],
            status=200,
        )
        service = BotTranscriptionService()
        assert service._resolve_profile({}, "org", {}) == "p-first"

    @responses.activate
    def test_empty_profiles_raises_no_quickmeeting_profile(self, studio_settings):
        """An org with no quickMeeting profile → NoQuickMeetingProfile (→ 409)."""
        responses.get(
            f"{STUDIO}/api/organizations/org/transcriber_profiles", json=[], status=200
        )
        service = BotTranscriptionService()
        with pytest.raises(NoQuickMeetingProfile):
            service._resolve_profile({}, "org", {})


class TestListProfiles:
    """list_profiles / list_profiles_for_room: shape normalization + org resolution."""

    @responses.activate
    def test_list_profiles_flattens_languages_and_translations(self, studio_settings):
        """languages (dict candidates) and dict-shaped translations are flattened."""
        responses.get(
            f"{STUDIO}/api/organizations/org/transcriber_profiles",
            json=[
                {
                    "id": "p-1",
                    "config": {
                        "name": "French",
                        "languages": [{"candidate": "fr"}, "en"],
                        "availableTranslations": {
                            "discrete": ["de"],
                            "external": ["es"],
                        },
                    },
                }
            ],
            status=200,
        )
        service = BotTranscriptionService()
        result = service.list_profiles("org", headers={})
        assert result == [
            {
                "id": "p-1",
                "name": "French",
                "languages": ["fr", "en"],
                "translations": ["de", "es"],
            }
        ]

    @responses.activate
    def test_list_profiles_for_room_resolves_org(self, studio_settings):
        """list_profiles_for_room resolves the org, then lists its profiles."""
        responses.get(
            f"{STUDIO}/api/organizations/",
            json=[{"_id": "org-x", "name": "Visio"}],
            status=200,
        )
        responses.get(
            f"{STUDIO}/api/organizations/org-x/transcriber_profiles",
            json=[{"id": "p-1", "config": {"name": "N"}}],
            status=200,
        )
        service = BotTranscriptionService()
        room = RoomFactory()
        result = service.list_profiles_for_room(room)
        assert [p["id"] for p in result] == ["p-1"]


class TestResolveConversationId:
    """resolve_conversation_id: from_session_id match, name fallback, not-found."""

    @responses.activate
    def test_match_by_from_session_id(self, studio_settings):
        """A conversation whose type.from_session_id matches wins."""
        responses.get(
            f"{STUDIO}/api/organizations/org/conversations",
            json=[
                {"_id": "c-other", "name": "n", "type": {"from_session_id": "other"}},
                {"_id": "c-hit", "name": "n", "type": {"from_session_id": "sess-1"}},
            ],
            status=200,
        )
        service = BotTranscriptionService()
        assert service.resolve_conversation_id("org", "sess-1", "n") == "c-hit"

    @responses.activate
    def test_fallback_to_exact_name(self, studio_settings):
        """With no session match, an exact-name match is returned."""
        responses.get(
            f"{STUDIO}/api/organizations/org/conversations",
            json=[{"_id": "c-name", "name": "linto-xyz", "type": {}}],
            status=200,
        )
        service = BotTranscriptionService()
        assert service.resolve_conversation_id("org", "sess-1", "linto-xyz") == "c-name"

    @responses.activate
    def test_not_found_returns_none(self, studio_settings):
        """No matching conversation → None (caller retries)."""
        responses.get(
            f"{STUDIO}/api/organizations/org/conversations", json=[], status=200
        )
        service = BotTranscriptionService()
        assert service.resolve_conversation_id("org", "sess-1", "n") is None


class TestBanner:
    """_set_banner: pushes/clears the dedicated LiveKit room-metadata flag."""

    def test_set_and_clear_banner(self, studio_settings):
        """active=True sets the flag; active=False clears it (best-effort)."""
        room = RoomFactory()
        user = UserFactory()
        service = BotTranscriptionService()
        # update_metadata is already @async_to_sync (called synchronously here).
        with mock.patch(
            "core.services.bot_transcription.RoomManagement.update_metadata",
        ) as meta:
            service._set_banner(room, True, user=user)
            service._set_banner(room, False)
        assert meta.call_count == 2
        # First call sets the active flag + the starter id.
        assert meta.call_args_list[0].args[0] == str(room.id)
        assert meta.call_args_list[0].args[1] == {
            "linto_transcription_status": "active",
            "linto_transcription_started_by": str(user.id),
        }
        # Second call clears it (empty patch + the keys in the removal list).
        assert meta.call_args_list[1].args[1] == {}
        assert meta.call_args_list[1].args[2] == [
            "linto_transcription_status",
            "linto_transcription_started_by",
        ]

    def test_banner_failure_is_swallowed(self, studio_settings):
        """A LiveKit metadata failure must never break start/stop."""
        room = RoomFactory()
        service = BotTranscriptionService()
        with mock.patch(
            "core.services.bot_transcription.RoomManagement.update_metadata",
            side_effect=RuntimeError("livekit down"),
        ):
            service._set_banner(room, True)  # no raise


class TestStartBot:
    """start_bot: permission gate, happy path, and bot-start rollback failure."""

    @pytest.fixture(autouse=True)
    def _no_livekit(self):
        """Silence the LiveKit side effects (banner metadata, native agent stop)."""
        with (
            mock.patch.object(BotTranscriptionService, "_set_banner") as banner,
            mock.patch.object(
                BotTranscriptionService, "_stop_native_subtitles"
            ) as stop_native,
        ):
            self.banner = banner
            self.stop_native = stop_native
            yield

    def test_permission_denied_for_non_admin(self, studio_settings):
        """transcript_permission defaults to admin_owner → a plain user is denied."""
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org"
        studio_settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = "prof"
        room = RoomFactory()
        user = UserFactory()  # not an admin/owner of the room
        service = BotTranscriptionService()
        with pytest.raises(BotTranscriptionException):  # PermissionDeniedError subclass
            service.start_bot(room, {}, user=user)
        assert (room.configuration or {}).get("linto") is None

    @responses.activate
    def test_happy_path_persists_config_and_returns_running(self, studio_settings):
        """Owner starts the bot: quickMeeting + bots are POSTed, config persisted."""
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org"
        studio_settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = "prof"
        studio_settings.LINTO_VISIO_NATIVE_ENABLED = True
        studio_settings.LINTO_NATIVE_LIVEKIT_URL = "ws://livekit:7880"
        quick = responses.post(
            f"{STUDIO}/api/organizations/org/quickMeeting/",
            json={"id": "sess-1", "channels": [{"id": "chan-1"}]},
            status=201,
        )
        meta_patch = responses.patch(
            f"{STUDIO}/api/organizations/org/sessions/sess-1", json={}, status=200
        )
        bots = responses.post(
            f"{STUDIO}/api/organizations/org/bots", json={"id": "bot-1"}, status=201
        )
        owner = UserFactory()
        room = RoomFactory()
        room.accesses.create(user=owner, role=models.RoleChoices.OWNER)
        service = BotTranscriptionService()

        with mock.patch(
            "core.services.bot_transcription.generate_bot_join_token",
            return_value="lk-jwt",
        ) as mint:
            result = service.start_bot(room, {"summary": True}, user=owner)

        assert result["status"] == "running"
        assert result["session_id"] == "sess-1"
        assert "live" not in result
        room.refresh_from_db()
        linto = room.configuration["linto"]
        assert linto["session_id"] == "sess-1"
        assert linto["channel_id"] == "chan-1"
        assert linto["bot_id"] == "bot-1"
        assert linto["user_id"] == str(owner.id)
        assert "live" not in linto

        # The native bot descriptor: declared on the quickMeeting, then completed
        # with the Meet-minted join token (identity carries room + channel).
        quick_body = json.loads(quick.calls[0].request.body)
        assert quick_body["meta"]["native"]["visio-native"] == {
            "livekitUrl": "ws://livekit:7880",
            "room": str(room.id),
        }
        mint.assert_called_once_with(str(room.id), "chan-1")
        patched = json.loads(meta_patch.calls[0].request.body)["meta"]
        assert patched["native"]["visio-native"]["token"] == "lk-jwt"
        assert patched["linto_native"]["token"] == "lk-jwt"
        assert json.loads(bots.calls[0].request.body)["provider"] == "visio"

        # LinTO takes over the captions: native agent stopped, banner lit.
        self.stop_native.assert_called_once_with(room)
        self.banner.assert_called_once_with(room, True, user=owner)

    @responses.activate
    def test_bot_start_failure_rolls_back_and_raises(self, studio_settings):
        """A failed /bots POST deletes the orphan session and raises."""
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org"
        studio_settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = "prof"
        responses.post(
            f"{STUDIO}/api/organizations/org/quickMeeting/",
            json={"id": "sess-1", "channels": [{"id": "chan-1"}]},
            status=201,
        )
        responses.post(f"{STUDIO}/api/organizations/org/bots", body="boom", status=500)
        rollback = responses.delete(
            f"{STUDIO}/api/organizations/org/quickMeeting/sess-1", status=200
        )
        owner = UserFactory()
        room = RoomFactory()
        room.accesses.create(user=owner, role=models.RoleChoices.OWNER)
        service = BotTranscriptionService()

        with pytest.raises(BotTranscriptionException):
            service.start_bot(room, {}, user=owner)
        assert rollback.call_count == 1
        room.refresh_from_db()
        assert (room.configuration or {}).get("linto") is None


class TestStopBot:
    """stop_bot: idle, permission gate, and happy path (finalize + enqueue summary)."""

    @pytest.fixture(autouse=True)
    def _no_banner(self):
        with mock.patch.object(BotTranscriptionService, "_set_banner"):
            yield

    def test_idle_when_no_bot(self, studio_settings):
        """No running bot → {"status": "idle"} with no upstream call."""
        room = RoomFactory()
        service = BotTranscriptionService()
        assert service.stop_bot(room) == {"status": "idle"}

    def test_permission_denied_for_stranger(self, studio_settings):
        """A non-starter, non-admin participant cannot stop someone else's bot."""
        starter = UserFactory()
        room = RoomFactory(
            configuration={
                "linto": {
                    "org_id": "org",
                    "session_id": "sess-1",
                    "user_id": str(starter.id),
                }
            }
        )
        stranger = UserFactory()
        service = BotTranscriptionService()
        with pytest.raises(BotTranscriptionException):  # PermissionDeniedError subclass
            service.stop_bot(room, user=stranger)

    @responses.activate
    def test_happy_path_finalizes_and_enqueues_summary(self, studio_settings):
        """Starter stops: bot + quickMeeting DELETEd, config cleared, summary enqueued."""
        responses.delete(f"{STUDIO}/api/organizations/org/bots/bot-1", status=200)
        responses.delete(
            f"{STUDIO}/api/organizations/org/quickMeeting/sess-1", status=200
        )
        starter = UserFactory()
        room = RoomFactory(
            configuration={
                "linto": {
                    "org_id": "org",
                    "session_id": "sess-1",
                    "bot_id": "bot-1",
                    "summary": True,
                    "user_id": str(starter.id),
                }
            }
        )
        service = BotTranscriptionService()
        with mock.patch("core.tasks.linto.process_bot_live_summary") as task:
            result = service.stop_bot(room, user=starter)
        assert result == {"status": "stopped"}
        task.delay.assert_called_once_with(str(room.id))
        room.refresh_from_db()
        assert "linto" not in room.configuration
        assert room.configuration["linto_summary"]["session_id"] == "sess-1"


class TestBotStatus:
    """bot_status: idle vs running (captions polled from Session-API)."""

    def test_idle(self, studio_settings):
        """No running bot → idle with empty captions."""
        room = RoomFactory()
        service = BotTranscriptionService()
        assert service.bot_status(room) == {"status": "idle", "captions": []}

    def test_running_without_session_api(self, studio_settings):
        """Running bot with no Session-API configured → captions safely empty."""
        studio_settings.LINTO_SESSION_API_URL = None
        room = RoomFactory(
            configuration={"linto": {"session_id": "sess-1", "org_id": "org"}}
        )
        service = BotTranscriptionService()
        payload = service.bot_status(room)
        assert payload["status"] == "running"
        assert payload["session_id"] == "sess-1"
        assert payload["captions"] == []
