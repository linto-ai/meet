"""
Test the LinTO live-transcription lifecycle service.

Studio HTTP is mocked; LiveKit side effects (banner metadata, native-agent
stop, token mint, egress) are patched.
"""

# pylint: disable=redefined-outer-name,protected-access

import json
from datetime import datetime, timezone
from unittest import mock

import pytest
import requests
import responses

from core import models
from core.factories import RoomFactory, UserFactory
from core.services.bot_transcription import (
    LINTO_CHANNEL_INDEX,
    ROOM_METADATA_CATCHUP_KEY,
    ROOM_METADATA_CHANNEL_ID_KEY,
    ROOM_METADATA_CHANNEL_INDEX_KEY,
    ROOM_METADATA_KEEP_AUDIO_KEY,
    ROOM_METADATA_KEYS,
    ROOM_METADATA_ORG_ID_KEY,
    ROOM_METADATA_SESSION_ID_KEY,
    ROOM_METADATA_STARTED_AT_KEY,
    ROOM_METADATA_STARTER_KEY,
    ROOM_METADATA_STATUS_KEY,
    BotTranscriptionException,
    BotTranscriptionService,
    NoProfileError,
    PermissionDeniedError,
    RunConflictError,
    StudioAudioNotReady,
    StudioNotEntitled,
    StudioRefusedError,
    TooManyTranslationsError,
    effective_token_source,
    normalize_profile,
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
        # Service account: every capability granted, the defaults apply.
        studio_settings.LINTO_ENTITLEMENTS_ENABLED = False
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
        assert linto["summary_service"] is None  # no choice → instance default
        assert linto["catchup"] is True  # late-joiner summary on by default
        assert "recording_id" not in linto  # record was False → no egress
        # The banner call carries the run state, so the metadata can advertise
        # the Studio ids a late joiner needs to catch up.
        self.banner.assert_called_once_with(room, True, user=owner, linto=linto)
        self.rec.assert_not_called()

    def test_keeps_what_the_previous_delivery_wrote_meanwhile(self, studio_settings):
        room, owner = _owner_room()
        delivery = {"session_id": "sess-0", "steps": {"tag": True}}
        models.Room.objects.filter(pk=room.pk).update(
            configuration={"linto_summary": delivery}
        )
        BotTranscriptionService().mark_started(
            room, {"session_id": "sess-1", "channel_id": "c"}, user=owner
        )
        room.refresh_from_db()
        assert room.configuration["linto_summary"] == delivery
        assert room.configuration["linto"]["session_id"] == "sess-1"

    def test_keeps_audio_when_the_instance_keeps_it(self, studio_settings):
        # LINTO_LIVE_KEEP_AUDIO on: the run keeps the audio, whatever the body.
        studio_settings.LINTO_LIVE_KEEP_AUDIO = True
        room, owner = _owner_room()
        data = {"session_id": "s", "channel_id": "c", "keep_audio": False}
        result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["keep_audio"] is True
        room.refresh_from_db()
        assert room.configuration["linto"]["keep_audio"] is True

    def test_keeps_audio_when_the_channel_keeps_it(self, studio_settings):
        # The browser created the channel with keepAudio: the indicator shows
        # even if the instance setting says otherwise.
        studio_settings.LINTO_LIVE_KEEP_AUDIO = False
        room, owner = _owner_room()
        data = {"session_id": "s", "channel_id": "c", "keep_audio": True}
        result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["keep_audio"] is True

    def test_text_only_run_keeps_no_audio(self, studio_settings):
        studio_settings.LINTO_LIVE_KEEP_AUDIO = False
        room, owner = _owner_room()
        for data in (
            {"session_id": "s", "channel_id": "c"},
            {"session_id": "s", "channel_id": "c", "keep_audio": False},
            {"session_id": "s", "channel_id": "c", "keep_audio": "yes"},
        ):
            result = BotTranscriptionService().mark_started(room, data, user=owner)
            assert result["keep_audio"] is False
        room.refresh_from_db()
        assert room.configuration["linto"]["keep_audio"] is False

    def test_persists_the_late_joiner_summary_choice(self, studio_settings):
        room, owner = _owner_room()
        data = {"session_id": "s", "channel_id": "c", "catchup": False}
        result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["catchup"] is False
        room.refresh_from_db()
        assert room.configuration["linto"]["catchup"] is False

    def test_persists_the_chosen_summary_service(self, studio_settings):
        room, owner = _owner_room()
        data = {"session_id": "s", "channel_id": "c", "summary_service": "minutes"}
        result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["summary_service"] == "minutes"
        room.refresh_from_db()
        assert room.configuration["linto"]["summary_service"] == "minutes"
        # Anything but a string is ignored.
        data["summary_service"] = {"route": "x"}
        result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["summary_service"] is None

    def test_record_add_on_dropped_without_screen_permission(self, studio_settings):
        # transcript authenticated (any logged user), screen_recording admin_owner.
        room = RoomFactory(configuration={"transcript_permission": "authenticated"})
        user = UserFactory()  # logged in but not admin/owner
        result = BotTranscriptionService().mark_started(
            room, {"session_id": "s", "channel_id": "c", "record": True}, user=user
        )
        assert result["record"] is False
        self.rec.assert_not_called()

    def test_record_add_on_dropped_without_the_recording_capability(
        self, studio_settings
    ):
        """LINTO_RECORDING_ENTITLEMENT_ENABLED: the add-on needs `recording`."""
        studio_settings.LINTO_FEATURE_ENABLED = True
        studio_settings.LINTO_RECORDING_ENTITLEMENT_ENABLED = True
        room, owner = _owner_room()
        data = {"session_id": "s", "channel_id": "c", "record": True}
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities",
            return_value={"recording": False},
        ):
            result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["record"] is False
        self.rec.assert_not_called()
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities",
            return_value={"recording": True},
        ):
            result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["record"] is True
        self.rec.assert_called_once()

    def test_summary_dropped_without_the_summary_capability(self, studio_settings):
        """Per-user gating on: the end-of-meeting summary needs `summary`,
        whatever the browser sends; absent counts as not granted."""
        studio_settings.LINTO_FEATURE_ENABLED = True
        studio_settings.LINTO_ENTITLEMENTS_ENABLED = True
        room, owner = _owner_room()
        data = {"session_id": "s", "channel_id": "c", "org_id": "o", "summary": True}
        for capabilities in (
            {"transcription": {"live": True, "async": True}, "recording": True},
            {"transcription": {"live": True}, "summary": False},
        ):
            with mock.patch(
                "core.entitlements.capabilities.user_linto_capabilities",
                return_value=capabilities,
            ):
                result = BotTranscriptionService().mark_started(room, data, user=owner)
            assert result["summary"] is False
            room.refresh_from_db()
            assert room.configuration["linto"]["summary"] is False
        # The run is still delivered at stop, without the summary step.
        with (
            mock.patch.object(BotTranscriptionService, "_stop_studio_run"),
            mock.patch.object(BotTranscriptionService, "_enqueue_summary") as enqueue,
        ):
            BotTranscriptionService().stop(room, user=owner)
        enqueue.assert_called_once_with(room)
        room.refresh_from_db()
        assert room.configuration["linto_summary"]["summary"] is False

    def test_catchup_dropped_without_the_summary_capability(self, studio_settings):
        """The late joiners' summary is a summary too: off without `summary`,
        and advertised as off to the room."""
        studio_settings.LINTO_FEATURE_ENABLED = True
        studio_settings.LINTO_ENTITLEMENTS_ENABLED = True
        room, owner = _owner_room()
        data = {"session_id": "s", "channel_id": "c", "summary": False}
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities",
            return_value={"transcription": {"live": True}},
        ):
            result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["catchup"] is False
        linto = self.banner.call_args.kwargs["linto"]
        assert linto["catchup"] is False
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities",
            return_value={"transcription": {"live": True}, "summary": True},
        ):
            result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["catchup"] is True

    def test_summary_kept_with_the_summary_capability(self, studio_settings):
        studio_settings.LINTO_FEATURE_ENABLED = True
        studio_settings.LINTO_ENTITLEMENTS_ENABLED = True
        room, owner = _owner_room()
        data = {"session_id": "s", "channel_id": "c", "org_id": "o"}
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities",
            return_value={"transcription": {"live": True}, "summary": True},
        ):
            result = BotTranscriptionService().mark_started(room, data, user=owner)
        assert result["summary"] is True
        with (
            mock.patch.object(BotTranscriptionService, "_stop_studio_run"),
            mock.patch.object(BotTranscriptionService, "_enqueue_summary") as enqueue,
        ):
            BotTranscriptionService().stop(room, user=owner)
        enqueue.assert_called_once_with(room)
        room.refresh_from_db()
        assert room.configuration["linto_summary"]["summary"] is True

    def test_summary_untouched_with_the_kill_switch_off(self, studio_settings):
        """LINTO_ENTITLEMENTS_ENABLED=False: no lookup, the browser's choice
        stands as before."""
        studio_settings.LINTO_FEATURE_ENABLED = True
        studio_settings.LINTO_ENTITLEMENTS_ENABLED = False
        room, owner = _owner_room()
        with mock.patch(
            "core.entitlements.capabilities.user_linto_capabilities"
        ) as capabilities:
            on = BotTranscriptionService().mark_started(
                room, {"session_id": "s", "channel_id": "c"}, user=owner
            )
            off = BotTranscriptionService().mark_started(
                room,
                {"session_id": "s", "channel_id": "c", "summary": False},
                user=owner,
            )
        capabilities.assert_not_called()
        assert on["summary"] is True
        assert on["catchup"] is True
        assert off["summary"] is False


class TestBannerMetadata:
    """_set_banner: the room-metadata contract read by EVERY participant.

    Beyond the status/starter pair, an active run advertises the Studio session
    identity (+ start timestamp) so a LATE JOINER's browser can fetch the
    transcript so far and the "before you arrived" summary. Everything is
    cleared as one block at stop/teardown.
    """

    def test_active_publishes_the_session_identity(self, studio_settings):
        # Service account: every capability granted, the defaults apply.
        studio_settings.LINTO_ENTITLEMENTS_ENABLED = False
        room, owner = _owner_room()
        data = {
            "session_id": "sess-1",
            "channel_id": "chan-1",
            "org_id": "org-1",
            "bot_id": "bot-1",
            "record": False,
        }
        with (
            mock.patch(
                "core.services.bot_transcription.RoomManagement"
            ) as room_management,
            mock.patch.object(BotTranscriptionService, "_start_recording"),
        ):
            BotTranscriptionService().mark_started(room, data, user=owner)
        room_management.return_value.update_metadata.assert_called_once()
        args = room_management.return_value.update_metadata.call_args[0]
        assert args[0] == str(room.id)
        metadata = args[1]
        assert metadata[ROOM_METADATA_STATUS_KEY] == "active"
        assert metadata[ROOM_METADATA_STARTER_KEY] == str(owner.id)
        assert metadata[ROOM_METADATA_SESSION_ID_KEY] == "sess-1"
        assert metadata[ROOM_METADATA_CHANNEL_ID_KEY] == "chan-1"
        assert metadata[ROOM_METADATA_CHANNEL_INDEX_KEY] == LINTO_CHANNEL_INDEX
        assert metadata[ROOM_METADATA_ORG_ID_KEY] == "org-1"
        assert metadata[ROOM_METADATA_CATCHUP_KEY] == "1"
        # LINTO_LIVE_KEEP_AUDIO defaults to True: the run keeps the audio.
        assert metadata[ROOM_METADATA_KEEP_AUDIO_KEY] == "1"
        started_at = metadata[ROOM_METADATA_STARTED_AT_KEY]
        # ISO 8601 UTC, parseable by the browser's Date.parse.
        assert started_at.endswith("Z")
        parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60

    def test_active_without_ids_only_publishes_the_status(self, studio_settings):
        # A half-written key set would send the catch-up hook after a session
        # that does not exist: publish the ids only when we have them.
        room, owner = _owner_room()
        with mock.patch(
            "core.services.bot_transcription.RoomManagement"
        ) as room_management:
            BotTranscriptionService()._set_banner(room, True, user=owner, linto={})
        metadata = room_management.return_value.update_metadata.call_args[0][1]
        assert set(metadata) == {
            ROOM_METADATA_STATUS_KEY,
            ROOM_METADATA_STARTER_KEY,
            ROOM_METADATA_CATCHUP_KEY,
            ROOM_METADATA_KEEP_AUDIO_KEY,
        }

    def test_kept_audio_is_advertised(self, studio_settings):
        # Every participant's recording indicator reads this key.
        room, owner = _owner_room()
        with mock.patch(
            "core.services.bot_transcription.RoomManagement"
        ) as room_management:
            BotTranscriptionService()._set_banner(
                room, True, user=owner, linto={"keep_audio": True}
            )
        metadata = room_management.return_value.update_metadata.call_args[0][1]
        assert metadata[ROOM_METADATA_KEEP_AUDIO_KEY] == "1"

    def test_text_only_run_advertises_no_kept_audio(self, studio_settings):
        # A run state without the key (started before it existed) is text only.
        room, owner = _owner_room()
        for linto in ({"keep_audio": False}, {}):
            with mock.patch(
                "core.services.bot_transcription.RoomManagement"
            ) as room_management:
                BotTranscriptionService()._set_banner(
                    room, True, user=owner, linto=linto
                )
            metadata = room_management.return_value.update_metadata.call_args[0][1]
            assert metadata[ROOM_METADATA_KEEP_AUDIO_KEY] == "0"

    def test_late_joiner_summary_off_is_advertised(self, studio_settings):
        room, owner = _owner_room()
        with mock.patch(
            "core.services.bot_transcription.RoomManagement"
        ) as room_management:
            BotTranscriptionService()._set_banner(
                room, True, user=owner, linto={"catchup": False}
            )
        metadata = room_management.return_value.update_metadata.call_args[0][1]
        assert metadata[ROOM_METADATA_CATCHUP_KEY] == "0"

    def test_inactive_removes_every_linto_key(self, studio_settings):
        room = RoomFactory()
        with mock.patch(
            "core.services.bot_transcription.RoomManagement"
        ) as room_management:
            BotTranscriptionService()._set_banner(room, False)
        args = room_management.return_value.update_metadata.call_args[0]
        assert args[1] == {}
        assert sorted(args[2]) == sorted(ROOM_METADATA_KEYS)
        assert ROOM_METADATA_SESSION_ID_KEY in args[2]
        # The recording indicator goes away with the run.
        assert ROOM_METADATA_KEEP_AUDIO_KEY in args[2]

    def test_metadata_failure_never_breaks_the_run(self, studio_settings):
        room, owner = _owner_room()
        with mock.patch(
            "core.services.bot_transcription.RoomManagement"
        ) as room_management:
            room_management.return_value.update_metadata.side_effect = RuntimeError(
                "livekit down"
            )
            BotTranscriptionService()._set_banner(
                room, True, user=owner, linto={"session_id": "sess-1"}
            )


class TestTeardown:
    """teardown: the stop without the permission gate."""

    @responses.activate
    def test_deletes_bot_and_session_then_clears(self, run_settings):
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
        del_session = responses.delete(
            f"{STUDIO}/api/organizations/org-1/sessions/sess-1",
            json={"success": True},
            status=200,
        )
        with (
            mock.patch.object(BotTranscriptionService, "_set_banner"),
            mock.patch.object(BotTranscriptionService, "_enqueue_summary"),
        ):
            BotTranscriptionService().teardown(room)
        assert del_bot.call_count == 1
        assert del_session.call_count == 1
        assert "name=linto-" in del_session.calls[0].request.url
        room.refresh_from_db()
        assert "linto" not in room.configuration

    @responses.activate
    def test_clears_the_room_metadata(self, run_settings):
        # A room ending with a run still active must leave no stale session id
        # behind, or a later joiner would try to catch up on a dead session.
        room = RoomFactory()
        room.configuration = {
            "linto": {"org_id": "org-1", "session_id": "sess-1", "summary": False}
        }
        room.save()
        responses.delete(
            f"{STUDIO}/api/organizations/org-1/sessions/sess-1",
            json={"success": True},
            status=200,
        )
        with (
            mock.patch(
                "core.services.bot_transcription.RoomManagement"
            ) as room_management,
            mock.patch.object(BotTranscriptionService, "_enqueue_summary"),
        ):
            BotTranscriptionService().teardown(room)
        args = room_management.return_value.update_metadata.call_args[0]
        assert args[1] == {}
        assert sorted(args[2]) == sorted(ROOM_METADATA_KEYS)
        # The room emptied: the recording indicator is cleared with the run.
        assert ROOM_METADATA_KEEP_AUDIO_KEY in args[2]


class TestStudioTokenFor:
    """The backend's Studio credential (LINTO_STUDIO_TOKEN_SOURCE)."""

    def test_service_account_with_pinned_org(self, studio_settings):
        studio_settings.LINTO_STUDIO_TOKEN_SOURCE = "service_account"
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org-pin"
        result = BotTranscriptionService().studio_token_for(UserFactory())
        assert result == {
            "enabled": True,
            "token": "static-token",
            "organization_id": "org-pin",
            "capabilities": {"quickMeeting": True},
        }

    @responses.activate
    def test_service_account_falls_back_to_the_first_org(self, studio_settings):
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = ""
        responses.add(
            responses.GET,
            f"{STUDIO}/api/organizations/",
            json=[{"_id": "first", "name": "Whatever"}, {"_id": "second"}],
        )
        result = BotTranscriptionService().studio_token_for(UserFactory())
        assert result["organization_id"] == "first"

    @responses.activate
    def test_service_account_without_any_org_raises(self, studio_settings):
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = ""
        responses.add(responses.GET, f"{STUDIO}/api/organizations/", json=[])
        with pytest.raises(BotTranscriptionException):
            BotTranscriptionService().studio_token_for(UserFactory())

    def test_raises_without_credentials(self, studio_settings):
        studio_settings.LINTO_STUDIO_API_TOKEN = None
        with pytest.raises(BotTranscriptionException):
            BotTranscriptionService().studio_token_for(UserFactory())

    def test_unknown_source_raises(self, studio_settings):
        # The token source is only read when the per-user rights are on.
        studio_settings.LINTO_ENTITLEMENTS_ENABLED = True
        studio_settings.LINTO_STUDIO_TOKEN_SOURCE = "keycloak"
        with pytest.raises(
            BotTranscriptionException, match="LINTO_STUDIO_TOKEN_SOURCE"
        ):
            BotTranscriptionService().studio_token_for(UserFactory())

    def test_integration_token_is_the_preferred_credential(self, studio_settings):
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org-pin"
        studio_settings.LINTO_STUDIO_INTEGRATION_TOKEN = "integration-key"
        result = BotTranscriptionService().studio_token_for(UserFactory())
        assert result["token"] == "integration-key"


@pytest.fixture
def user_key_settings(studio_settings):
    # user_key only exists with the per-user rights on (off by default).
    studio_settings.LINTO_ENTITLEMENTS_ENABLED = True
    studio_settings.LINTO_STUDIO_TOKEN_SOURCE = "user_key"
    studio_settings.LINTO_IDENTITY_PROVIDER = "meet:test"
    studio_settings.LINTO_STUDIO_INTEGRATION_TOKEN = "integration-key"
    studio_settings.LINTO_STUDIO_TOKEN_CACHE_TTL = 300
    studio_settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    from django.core.cache import cache  # noqa: PLC0415

    cache.clear()
    return studio_settings


EXCHANGE = f"{STUDIO}/api/auth/external/token"


class TestUserKeyToken:
    """user_key mode: the studio-api identity exchange, per user, cached."""

    @responses.activate
    def test_exchanges_the_identity_for_the_users_key_token(self, user_key_settings):
        user = UserFactory(sub="lemon-sub-42", email="Alice@Linagora.com")
        responses.add(
            responses.POST,
            EXCHANGE,
            json={
                "token": "short-jwt",
                "expiresIn": 3600,
                "userId": "key-user",
                "organizationId": "org-of-the-key",
                "capabilities": {"quickMeeting": True},
            },
        )
        result = BotTranscriptionService().studio_token_for(user)
        assert result == {
            "enabled": True,
            "token": "short-jwt",
            "organization_id": "org-of-the-key",
            "expires_in": 3600,
            "capabilities": {"quickMeeting": True},
        }
        call = responses.calls[0].request
        assert call.headers["Authorization"] == "Bearer integration-key"
        assert "userScope=backoffice" in call.url
        assert json.loads(call.body) == {
            "provider": "meet:test",
            "subject": "lemon-sub-42",
            "email": "alice@linagora.com",
        }

    @responses.activate
    def test_result_is_cached_per_user(self, user_key_settings):
        user = UserFactory(sub="s1")
        other = UserFactory(sub="s2")
        responses.add(responses.POST, EXCHANGE, json={"token": "t", "expiresIn": 3600})
        service = BotTranscriptionService()
        service.studio_token_for(user)
        service.studio_token_for(user)
        assert len(responses.calls) == 1
        service.studio_token_for(other)
        assert len(responses.calls) == 2
        service.forget_user_token(user)
        service.studio_token_for(user)
        assert len(responses.calls) == 3

    @responses.activate
    @pytest.mark.parametrize("code", ["no_entitlement", "no_linked_key"])
    def test_no_entitlement_means_not_entitled_and_is_cached(
        self, user_key_settings, code
    ):
        user = UserFactory(sub="nobody")
        responses.add(responses.POST, EXCHANGE, status=404, json={"code": code})
        service = BotTranscriptionService()
        assert service.studio_token_for(user) == {
            "enabled": False,
            "reason": code,
        }
        assert service.studio_token_for(user)["enabled"] is False
        assert len(responses.calls) == 1

    @responses.activate
    @pytest.mark.parametrize("code", ["revoked", "domain_inactive"])
    def test_revoked_or_inactive_domain_is_not_entitled(self, user_key_settings, code):
        user = UserFactory(sub="gone")
        responses.add(responses.POST, EXCHANGE, status=403, json={"code": code})
        result = BotTranscriptionService().studio_token_for(user)
        assert result == {"enabled": False, "reason": code}

    @responses.activate
    def test_other_failures_raise(self, user_key_settings):
        user = UserFactory(sub="x")
        responses.add(responses.POST, EXCHANGE, status=401, json={"message": "nope"})
        with pytest.raises(BotTranscriptionException, match="401"):
            BotTranscriptionService().studio_token_for(user)
        responses.replace(responses.POST, EXCHANGE, status=500, body="boom")
        with pytest.raises(BotTranscriptionException, match="500"):
            BotTranscriptionService().studio_token_for(user)

    @responses.activate
    def test_cache_ttl_never_outlives_the_token(self, user_key_settings):
        user = UserFactory(sub="short")
        responses.add(responses.POST, EXCHANGE, json={"token": "t", "expiresIn": 90})
        with mock.patch("core.services.bot_transcription.cache") as fake_cache:
            fake_cache.get.return_value = None
            BotTranscriptionService().studio_token_for(user)
        assert fake_cache.set.call_args.args[2] == 30


SERVICES_URL = f"{STUDIO}/api/services/org-pin/llm"


class TestSummaryServices:
    """summary_services: the gateway services carrying the `meet` scope, listed
    through Studio under the service account, cached, never failing."""

    @pytest.fixture(autouse=True)
    def _pinned_org(self, studio_settings):
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org-pin"
        studio_settings.LINTO_LLM_SERVICE_ROUTE = None
        studio_settings.CACHES = {
            "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
        }
        from django.core.cache import cache as django_cache  # noqa: PLC0415

        django_cache.clear()

    @responses.activate
    def test_lists_the_meet_scoped_services_with_icon_and_default(
        self, studio_settings
    ):
        responses.add(
            responses.GET,
            SERVICES_URL,
            json=[
                {
                    "id": "1",
                    "name": "Meeting minutes",
                    "route": "minutes",
                    "service_type": "summary",
                    "description": {"en": "Minutes", "fr": "Compte rendu"},
                    "scopes": ["linto", "meet"],
                    "metadata": {"icon": "file-text"},
                },
                {
                    "id": "2",
                    "name": "Action items",
                    "route": "actions",
                    "service_type": "summary",
                    "description": {},
                    "scopes": ["meet"],
                    "metadata": {},
                },
            ],
        )
        services = BotTranscriptionService().summary_services()
        assert services == [
            {
                "route": "minutes",
                "name": "Meeting minutes",
                "description": {"en": "Minutes", "fr": "Compte rendu"},
                "icon": "file-text",
                "default": True,
            },
            {
                "route": "actions",
                "name": "Action items",
                "description": {},
                "icon": None,
                "default": False,
            },
        ]
        call = responses.calls[0].request
        assert call.headers["Authorization"] == "Bearer static-token"
        assert "scope=meet" in call.url

    @responses.activate
    def test_pinned_route_is_the_default_and_the_list_is_cached(self, studio_settings):
        studio_settings.LINTO_LLM_SERVICE_ROUTE = "actions"
        responses.add(
            responses.GET,
            SERVICES_URL,
            json=[
                {"name": "Minutes", "route": "minutes"},
                {"name": "Actions", "route": "actions"},
            ],
        )
        service = BotTranscriptionService()
        first = service.summary_services()
        assert [s["default"] for s in first] == [False, True]
        service.summary_services()
        assert len(responses.calls) == 1

    @responses.activate
    def test_studio_failure_means_no_choice(self, studio_settings):
        responses.add(responses.GET, SERVICES_URL, status=503, body="down")
        assert BotTranscriptionService().summary_services() == []


ASR_SERVICES_URL = f"{STUDIO}/api/services"


class TestTranscriptionLanguages:
    """transcription_languages: the union of what the gateway's STT services
    advertise, `*` (automatic) first, cached, never failing."""

    @pytest.fixture(autouse=True)
    def _cache(self, studio_settings):
        studio_settings.CACHES = {
            "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
        }
        from django.core.cache import cache as django_cache  # noqa: PLC0415

        django_cache.clear()

    @responses.activate
    def test_unions_the_stt_languages_with_auto_first(self, studio_settings):
        responses.add(
            responses.GET,
            ASR_SERVICES_URL,
            json=[
                {"name": "whisper", "scope": ["stt"], "language": "fr,en,*"},
                {"name": "kaldi-fr", "scope": ["stt"], "language": "fr-FR"},
                {"name": "kaldi-de", "language": "de"},
                # NLP services are typed and never offer a language.
                {"name": "punct", "scope": ["nlp"], "language": "fr"},
                {"name": "diar", "desc": {"type": "diarization"}, "language": "en"},
            ],
        )
        languages = BotTranscriptionService().transcription_languages()
        assert languages == ["*", "de", "en", "fr", "fr-FR"]
        assert responses.calls[0].request.headers["Authorization"] == (
            "Bearer static-token"
        )

    @responses.activate
    def test_no_auto_when_no_service_detects_and_the_list_is_cached(
        self, studio_settings
    ):
        responses.add(
            responses.GET,
            ASR_SERVICES_URL,
            json=[{"name": "kaldi", "scope": ["stt"], "language": "fr"}],
        )
        service = BotTranscriptionService()
        assert service.transcription_languages() == ["fr"]
        service.transcription_languages()
        assert len(responses.calls) == 1

    @responses.activate
    def test_studio_failure_means_automatic_only(self, studio_settings):
        responses.add(responses.GET, ASR_SERVICES_URL, status=503, body="down")
        assert BotTranscriptionService().transcription_languages() == []


class TestEntitlementsKillSwitch:
    """LINTO_ENTITLEMENTS_ENABLED=False: the per-user feature system is off —
    the token comes from the service account whatever the configured source,
    and the identity exchange is never called."""

    @responses.activate
    def test_gating_off_forces_the_service_account(self, user_key_settings):
        user_key_settings.LINTO_ENTITLEMENTS_ENABLED = False
        user_key_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org-linagora"
        user = UserFactory(sub="anyone")
        result = BotTranscriptionService().studio_token_for(user)
        assert result["enabled"] is True
        assert result["organization_id"] == "org-linagora"
        assert result["token"] == "integration-key"
        assert not any(c.request.url == EXCHANGE for c in responses.calls)

    def test_gating_on_keeps_the_configured_source(self, user_key_settings):
        user_key_settings.LINTO_ENTITLEMENTS_ENABLED = True
        assert effective_token_source() == "user_key"
        user_key_settings.LINTO_ENTITLEMENTS_ENABLED = False
        assert effective_token_source() == "service_account"


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


class TestConversationAudio:
    """The audio Studio kept for a live run (GET/HEAD .../media)."""

    MEDIA = f"{STUDIO}/api/conversations/conv-1/media"

    @responses.activate
    def test_fetch_returns_the_file(self, studio_settings):
        responses.get(self.MEDIA, body=b"ID3mp3", status=200)
        got = BotTranscriptionService().fetch_conversation_audio("conv-1")
        assert got == b"ID3mp3"
        assert responses.calls[0].request.headers["Authorization"] == (
            "Bearer static-token"
        )

    @responses.activate
    def test_no_audio_kept_is_none(self, studio_settings):
        responses.get(self.MEDIA, status=204)
        responses.head(self.MEDIA, status=204)
        service = BotTranscriptionService()
        assert service.fetch_conversation_audio("conv-1") is None
        assert service.conversation_audio_size("conv-1") is None

    @responses.activate
    def test_file_not_written_yet_is_not_ready(self, studio_settings):
        responses.get(self.MEDIA, status=404)
        responses.head(self.MEDIA, status=404)
        service = BotTranscriptionService()
        with pytest.raises(StudioAudioNotReady):
            service.fetch_conversation_audio("conv-1")
        with pytest.raises(StudioAudioNotReady):
            service.conversation_audio_size("conv-1")

    @responses.activate
    def test_other_failure_raises(self, studio_settings):
        responses.get(self.MEDIA, status=500)
        with pytest.raises(BotTranscriptionException) as excinfo:
            BotTranscriptionService().fetch_conversation_audio("conv-1")
        assert not isinstance(excinfo.value, StudioAudioNotReady)

    @responses.activate
    def test_size_is_read_from_a_head(self, studio_settings):
        responses.head(self.MEDIA, status=200, headers={"Content-Length": "1234"})
        assert BotTranscriptionService().conversation_audio_size("conv-1") == 1234
        assert responses.calls[0].request.method == "HEAD"


ORG = "org-1"
PROFILES_URL = f"{STUDIO}/api/organizations/{ORG}/transcriber_profiles"
SESSIONS_URL = f"{STUDIO}/api/organizations/{ORG}/sessions/"
BOTS_URL = f"{STUDIO}/api/organizations/{ORG}/bots"
CATCHUP_STATUS_URL = f"{STUDIO}/api/sessions/public/sess-1/catchup/status"
PROFILES = [
    {
        "id": 220,
        "name": "Whisper FR",
        "config": {"languages": [{"candidate": "fr-FR"}]},
        "translations": ["en", "de"],
    },
    {"id": 221, "name": "Other", "config": {}},
]


@pytest.fixture
def run_settings(studio_settings):
    studio_settings.LINTO_ENTITLEMENTS_ENABLED = False
    studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = ORG
    studio_settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = ""
    studio_settings.LINTO_VISIO_NATIVE_ENABLED = True
    studio_settings.LINTO_LIVE_KEEP_AUDIO = True
    studio_settings.LINTO_BOT_PROVIDER = "visio"
    studio_settings.EMAIL_APP_BASE_URL = "https://meet.example"
    studio_settings.LINTO_STUDIO_MEMBERS_RIGHT = 0
    studio_settings.LINTO_LIVE_MAX_TRANSLATIONS = 0
    studio_settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    from django.core.cache import cache  # noqa: PLC0415

    cache.clear()
    return studio_settings


def _mock_studio_start(session_id="sess-1", channel_id=7, bot_id="bot-1", catchup=None):
    responses.get(PROFILES_URL, json=PROFILES)
    responses.post(
        SESSIONS_URL,
        json={"id": session_id, "channels": [{"id": channel_id, "index": 0}]},
    )
    responses.patch(f"{STUDIO}/api/organizations/{ORG}/sessions/{session_id}", json={})
    responses.post(BOTS_URL, json={"id": bot_id})
    responses.get(
        f"{STUDIO}/api/sessions/public/{session_id}/catchup/status",
        **(catchup or {"json": {"enabled": True}}),
    )


def _calls(method, url_prefix):
    return [
        c.request
        for c in responses.calls
        if c.request.method == method and c.request.url.startswith(url_prefix)
    ]


class TestNormalizeProfile:
    def test_shapes_a_studio_profile_for_the_panel(self):
        assert normalize_profile(PROFILES[0]) == {
            "id": "220",
            "name": "Whisper FR",
            "languages": ["fr-FR"],
            "translations": ["en", "de"],
        }

    def test_translations_grouped_by_kind_are_flattened(self):
        profile = {
            "_id": "p",
            "config": {
                "name": "From config",
                "availableTranslations": {"discrete": ["en"], "external": ["it"]},
            },
        }
        assert normalize_profile(profile) == {
            "id": "p",
            "name": "From config",
            "languages": [],
            "translations": ["en", "it"],
        }


class TestQuickMeetingProfiles:
    @responses.activate
    def test_lists_the_live_profiles_of_the_organization(self, run_settings):
        responses.get(PROFILES_URL, json={"transcriber_profiles": PROFILES})
        profiles = BotTranscriptionService().quick_meeting_profiles(UserFactory())
        assert [p["id"] for p in profiles] == ["220", "221"]
        request = responses.calls[0].request
        assert "quickMeeting=true" in request.url
        assert request.headers["Authorization"] == "Bearer static-token"

    @responses.activate
    def test_studio_failure_raises(self, run_settings):
        responses.get(PROFILES_URL, status=500, body="down")
        with pytest.raises(BotTranscriptionException):
            BotTranscriptionService().quick_meeting_profiles(UserFactory())

    @responses.activate
    def test_not_entitled_user_key(self, user_key_settings):
        responses.post(EXCHANGE, status=404, json={"code": "no_entitlement"})
        with pytest.raises(StudioNotEntitled) as exc:
            BotTranscriptionService().quick_meeting_profiles(UserFactory(sub="x"))
        assert exc.value.reason == "no_entitlement"

    @responses.activate
    def test_a_key_without_quick_meeting_is_not_entitled(self, user_key_settings):
        responses.post(
            EXCHANGE,
            json={
                "token": "t",
                "organizationId": ORG,
                "capabilities": {"quickMeeting": False},
            },
        )
        with pytest.raises(StudioNotEntitled) as exc:
            BotTranscriptionService().quick_meeting_profiles(UserFactory(sub="y"))
        assert exc.value.reason == "quick_meeting_disabled"


class TestStart:
    @pytest.fixture(autouse=True)
    def _no_livekit(self):
        with (
            mock.patch.object(BotTranscriptionService, "_set_banner") as banner,
            mock.patch.object(
                BotTranscriptionService, "_stop_native_subtitles"
            ) as stop_native,
            mock.patch(
                "core.services.bot_transcription.generate_bot_join_token",
                return_value="lk-jwt",
            ) as mint,
        ):
            self.banner = banner
            self.stop_native = stop_native
            self.mint = mint
            yield

    @responses.activate
    def test_creates_the_rooms_session_and_launches_the_bot(self, run_settings):
        room, owner = _owner_room()
        _mock_studio_start()
        result = BotTranscriptionService().start(
            room,
            {"translations": ["en"], "summary": False, "catchup": True},
            user=owner,
        )

        (create,) = _calls("POST", SESSIONS_URL)
        body = json.loads(create.body)
        assert body["name"] == f"@meet-{room.id}"
        assert body["visibility"] == "public"
        assert body["channels"] == [
            {
                "name": "Main",
                "transcriberProfileId": 220,
                "enableLiveTranscripts": True,
                "diarization": True,
                "keepAudio": True,
                "translations": ["en"],
            }
        ]
        native = {"livekitUrl": "ws://livekit:7880", "room": str(room.id)}
        assert body["meta"]["native"] == {"visio-native": native}
        assert body["meta"]["linto_native"] == native

        (patch,) = _calls("PATCH", f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1")
        with_token = {**native, "token": "lk-jwt"}
        assert json.loads(patch.body)["meta"]["native"] == {"visio-native": with_token}
        self.mint.assert_called_once_with(str(room.id), "7")
        self.stop_native.assert_called_once_with(room)

        (bot,) = _calls("POST", BOTS_URL)
        assert json.loads(bot.body) == {
            "url": f"https://meet.example/{room.slug}",
            "channelId": 7,
            "provider": "visio",
            "enableDisplaySub": True,
            "subSource": "original",
        }
        assert not any("/quickMeeting/" in c.request.url for c in responses.calls)
        assert all(
            c.request.headers["Authorization"] == "Bearer static-token"
            for c in responses.calls
        )

        assert result["status"] == "running"
        room.refresh_from_db()
        linto = room.configuration["linto"]
        assert linto["session_id"] == "sess-1"
        assert linto["channel_id"] == 7
        assert linto["org_id"] == ORG
        assert linto["bot_id"] == "bot-1"
        assert linto["summary"] is False
        assert linto["catchup"] is True
        assert linto["keep_audio"] is True
        assert linto["user_id"] == str(owner.id)
        self.banner.assert_called_once()

    @responses.activate
    @pytest.mark.parametrize("members_right", [0, 1])
    def test_the_members_right_is_set_at_creation_and_kept_by_the_patch(
        self, run_settings, members_right
    ):
        run_settings.LINTO_STUDIO_MEMBERS_RIGHT = members_right
        room, owner = _owner_room()
        _mock_studio_start()
        BotTranscriptionService().start(room, {}, user=owner)
        (create,) = _calls("POST", SESSIONS_URL)
        assert json.loads(create.body)["meta"]["membersRight"] == members_right
        (patch,) = _calls("PATCH", f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1")
        meta = json.loads(patch.body)["meta"]
        assert meta["membersRight"] == members_right
        assert meta["native"]["visio-native"]["token"] == "lk-jwt"

    @responses.activate
    def test_the_members_right_without_the_native_bot(self, run_settings):
        run_settings.LINTO_VISIO_NATIVE_ENABLED = False
        room, owner = _owner_room()
        _mock_studio_start()
        BotTranscriptionService().start(room, {}, user=owner)
        (create,) = _calls("POST", SESSIONS_URL)
        assert json.loads(create.body)["meta"] == {"membersRight": 0}
        assert not _calls("PATCH", f"{STUDIO}/api/organizations/{ORG}/sessions/")

    @responses.activate
    def test_two_rooms_run_at_the_same_time_under_the_same_key(self, run_settings):
        first, first_owner = _owner_room()
        second, second_owner = _owner_room()
        responses.get(PROFILES_URL, json=PROFILES)
        responses.post(SESSIONS_URL, json={"id": "sess-a", "channels": [{"id": 1}]})
        responses.post(SESSIONS_URL, json={"id": "sess-b", "channels": [{"id": 2}]})
        responses.patch(f"{STUDIO}/api/organizations/{ORG}/sessions/sess-a", json={})
        responses.patch(f"{STUDIO}/api/organizations/{ORG}/sessions/sess-b", json={})
        responses.post(BOTS_URL, json={"id": "bot-a"})
        responses.post(BOTS_URL, json={"id": "bot-b"})

        service = BotTranscriptionService()
        service.start(first, {}, user=first_owner)
        service.start(second, {}, user=second_owner)

        names = [json.loads(c.body)["name"] for c in _calls("POST", SESSIONS_URL)]
        assert names == [f"@meet-{first.id}", f"@meet-{second.id}"]
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.configuration["linto"]["session_id"] == "sess-a"
        assert second.configuration["linto"]["session_id"] == "sess-b"

    @responses.activate
    def test_a_room_already_running_refuses_a_second_start(self, run_settings):
        room, owner = _owner_room()
        room.configuration = {"linto": {"session_id": "sess-1"}}
        room.save()
        with pytest.raises(RunConflictError) as exc:
            BotTranscriptionService().start(room, {}, user=owner)
        assert exc.value.code == "already_running"
        assert not responses.calls

    @responses.activate
    def test_a_start_under_way_refuses_another(self, run_settings):
        room, owner = _owner_room()
        from django.core.cache import cache  # noqa: PLC0415

        cache.add(f"linto:start:{room.id}", "1", 60)
        with pytest.raises(RunConflictError) as exc:
            BotTranscriptionService().start(room, {}, user=owner)
        assert exc.value.code == "already_starting"
        assert not responses.calls

    @responses.activate
    def test_the_lock_is_released_after_a_failure(self, run_settings):
        room, owner = _owner_room()
        responses.get(PROFILES_URL, status=503, body="down")
        with pytest.raises(BotTranscriptionException):
            BotTranscriptionService().start(room, {}, user=owner)
        responses.replace(responses.GET, PROFILES_URL, json=PROFILES)
        _mock_studio_start()
        assert BotTranscriptionService().start(room, {}, user=owner)["status"] == (
            "running"
        )

    @responses.activate
    def test_the_pinned_profile_is_used_when_the_organization_has_it(
        self, run_settings
    ):
        run_settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = "221"
        room, owner = _owner_room()
        _mock_studio_start()
        BotTranscriptionService().start(room, {}, user=owner)
        (create,) = _calls("POST", SESSIONS_URL)
        assert json.loads(create.body)["channels"][0]["transcriberProfileId"] == 221

    @responses.activate
    def test_a_pinned_profile_gone_falls_back_to_the_first(self, run_settings):
        run_settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = "999"
        room, owner = _owner_room()
        _mock_studio_start()
        BotTranscriptionService().start(room, {}, user=owner)
        (create,) = _calls("POST", SESSIONS_URL)
        assert json.loads(create.body)["channels"][0]["transcriberProfileId"] == 220

    @responses.activate
    def test_no_profile_starts_nothing(self, run_settings):
        room, owner = _owner_room()
        responses.get(PROFILES_URL, json=[])
        with pytest.raises(NoProfileError):
            BotTranscriptionService().start(room, {}, user=owner)
        assert not _calls("POST", SESSIONS_URL)

    @responses.activate
    @pytest.mark.parametrize(
        ("limit", "asked", "sent"),
        [
            (0, ["en", "de", "es", "it", "nl"], ["en", "de", "es", "it", "nl"]),
            (2, ["en", "de"], ["en", "de"]),
            (2, ["en", "en", "de"], ["en", "de"]),
            (1, [], []),
        ],
    )
    def test_translations_within_the_limit(self, run_settings, limit, asked, sent):
        run_settings.LINTO_LIVE_MAX_TRANSLATIONS = limit
        room, owner = _owner_room()
        _mock_studio_start()
        BotTranscriptionService().start(room, {"translations": asked}, user=owner)
        (create,) = _calls("POST", SESSIONS_URL)
        assert json.loads(create.body)["channels"][0]["translations"] == sent

    @responses.activate
    def test_more_translations_than_the_limit_start_nothing(self, run_settings):
        run_settings.LINTO_LIVE_MAX_TRANSLATIONS = 2
        room, owner = _owner_room()
        with pytest.raises(TooManyTranslationsError) as exc:
            BotTranscriptionService().start(
                room, {"translations": ["en", "de", "es"]}, user=owner
            )
        assert exc.value.limit == 2
        assert not responses.calls
        room.refresh_from_db()
        assert not (room.configuration or {}).get("linto")
        _mock_studio_start()
        result = BotTranscriptionService().start(
            room, {"translations": ["en", "de"]}, user=owner
        )
        assert result["status"] == "running"

    @responses.activate
    @pytest.mark.parametrize(
        ("answer", "logged"),
        [
            (
                {"json": {"enabled": False}},
                "no LLM Gateway service for the catch-up (LLM_CATCHUP_SERVICE",
            ),
            ({"status": 404, "body": "Cannot GET"}, "Studio has no catch-up routes"),
            (
                {"status": 404, "json": {"code": "session_not_found"}},
                "answered 404 session_not_found",
            ),
            ({"status": 503, "body": "down"}, "answered 503 down"),
        ],
    )
    def test_an_unavailable_catchup_is_logged(
        self, run_settings, caplog, answer, logged
    ):
        room, owner = _owner_room()
        _mock_studio_start(catchup=answer)
        with caplog.at_level("WARNING"):
            result = BotTranscriptionService().start(room, {}, user=owner)
        assert result["status"] == "running"
        (check,) = _calls("GET", CATCHUP_STATUS_URL)
        assert check.headers["Authorization"] == "Bearer static-token"
        assert logged in caplog.text
        assert "late joiners get no summary" in caplog.text

    @responses.activate
    def test_an_available_catchup_logs_nothing(self, run_settings, caplog):
        room, owner = _owner_room()
        _mock_studio_start()
        with caplog.at_level("WARNING"):
            BotTranscriptionService().start(room, {}, user=owner)
        assert len(_calls("GET", CATCHUP_STATUS_URL)) == 1
        assert "catch-up" not in caplog.text

    @responses.activate
    def test_an_unreachable_catchup_status_never_fails_the_start(
        self, run_settings, caplog
    ):
        room, owner = _owner_room()
        _mock_studio_start(catchup={"body": requests.ConnectionError("refused")})
        with caplog.at_level("WARNING"):
            result = BotTranscriptionService().start(room, {}, user=owner)
        assert result["status"] == "running"
        assert "LinTO catch-up status unknown" in caplog.text

    @responses.activate
    def test_a_run_without_catchup_checks_nothing(self, run_settings):
        room, owner = _owner_room()
        _mock_studio_start()
        BotTranscriptionService().start(room, {"catchup": False}, user=owner)
        assert not _calls("GET", CATCHUP_STATUS_URL)

    @responses.activate
    def test_a_bot_failure_stops_the_session_it_created(self, run_settings):
        room, owner = _owner_room()
        responses.get(PROFILES_URL, json=PROFILES)
        responses.post(SESSIONS_URL, json={"id": "sess-1", "channels": [{"id": 7}]})
        responses.patch(f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1", json={})
        responses.post(BOTS_URL, status=500, json={"error": "no bot"})
        deleted = responses.delete(
            f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1", json={}
        )
        with pytest.raises(BotTranscriptionException):
            BotTranscriptionService().start(room, {}, user=owner)
        assert deleted.call_count == 1
        room.refresh_from_db()
        assert "linto" not in (room.configuration or {})
        self.banner.assert_not_called()

    @responses.activate
    @pytest.mark.parametrize(
        ("status", "body", "code"),
        [
            (402, {"code": "entitlement_required"}, "entitlement_required"),
            (
                403,
                {"code": "transcriber_profile_security_level"},
                "transcriber_profile_security_level",
            ),
            (403, "Forbidden", None),
        ],
    )
    def test_a_refused_creation_says_so(self, run_settings, status, body, code):
        room, owner = _owner_room()
        responses.get(PROFILES_URL, json=PROFILES)
        if isinstance(body, dict):
            responses.post(SESSIONS_URL, status=status, json=body)
        else:
            responses.post(SESSIONS_URL, status=status, body=body)
        with pytest.raises(StudioRefusedError) as exc:
            BotTranscriptionService().start(room, {}, user=owner)
        assert exc.value.status == status
        assert exc.value.code == code
        room.refresh_from_db()
        assert "linto" not in (room.configuration or {})

    @responses.activate
    def test_a_refused_bot_stops_the_session(self, run_settings):
        room, owner = _owner_room()
        responses.get(PROFILES_URL, json=PROFILES)
        responses.post(SESSIONS_URL, json={"id": "sess-1", "channels": [{"id": 7}]})
        responses.patch(f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1", json={})
        responses.post(BOTS_URL, status=403, json={"message": "no BOT permission"})
        deleted = responses.delete(
            f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1", json={}
        )
        with pytest.raises(StudioRefusedError):
            BotTranscriptionService().start(room, {}, user=owner)
        assert deleted.call_count == 1

    def test_denied_for_non_admin(self, run_settings):
        room = RoomFactory()
        with pytest.raises(PermissionDeniedError):
            BotTranscriptionService().start(room, {}, user=UserFactory())

    @responses.activate
    def test_user_key_acts_with_the_users_own_key(self, user_key_settings):
        user_key_settings.LINTO_VISIO_NATIVE_ENABLED = True
        user_key_settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = ""
        room, owner = _owner_room()
        responses.post(
            EXCHANGE,
            json={"token": "user-jwt", "expiresIn": 3600, "organizationId": ORG},
        )
        _mock_studio_start()
        with mock.patch(
            "core.services.bot_transcription.summary_entitled", return_value=True
        ):
            BotTranscriptionService().start(room, {}, user=owner)
        studio_calls = [
            c for c in responses.calls if not c.request.url.startswith(EXCHANGE)
        ]
        assert studio_calls
        assert all(
            c.request.headers["Authorization"] == "Bearer user-jwt"
            for c in studio_calls
        )

    @responses.activate
    def test_user_key_not_entitled_starts_nothing(self, user_key_settings):
        room, owner = _owner_room()
        responses.post(EXCHANGE, status=404, json={"code": "no_entitlement"})
        with pytest.raises(StudioNotEntitled):
            BotTranscriptionService().start(room, {}, user=owner)
        assert all(c.request.url.startswith(EXCHANGE) for c in responses.calls)


class TestStop:
    @pytest.fixture(autouse=True)
    def _no_livekit(self):
        with (
            mock.patch.object(BotTranscriptionService, "_set_banner") as banner,
            mock.patch.object(BotTranscriptionService, "_stop_recording"),
            mock.patch.object(BotTranscriptionService, "_enqueue_summary") as enqueue,
        ):
            self.banner = banner
            self.enqueue = enqueue
            yield

    @staticmethod
    def _running(room, starter_id):
        room.configuration = {
            "linto": {
                "user_id": starter_id,
                "org_id": ORG,
                "session_id": "sess-1",
                "bot_id": "bot-1",
                "summary": True,
            }
        }
        room.save()

    @responses.activate
    def test_stops_the_bot_and_the_session_then_delivers(self, run_settings):
        room = RoomFactory()
        starter = UserFactory()
        self._running(room, str(starter.id))
        del_bot = responses.delete(f"{BOTS_URL}/bot-1", json={})
        del_session = responses.delete(
            f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1", json={}
        )
        assert BotTranscriptionService().stop(room, user=starter) == {
            "status": "stopped"
        }
        assert del_bot.call_count == 1
        assert del_session.call_count == 1
        url = del_session.calls[0].request.url
        assert "force=true" in url
        room.refresh_from_db()
        name = room.configuration["linto_summary"]["conversation_name"]
        assert f"name={name}" in url
        assert name.startswith(f"linto-{room.id}-")
        assert "linto" not in room.configuration
        assert not any("/quickMeeting/" in c.request.url for c in responses.calls)
        self.banner.assert_called_once_with(room, False)
        self.enqueue.assert_called_once_with(room)

    @responses.activate
    def test_an_admin_stops_someone_elses_run(self, run_settings):
        room, admin = _owner_room()
        self._running(room, str(UserFactory().id))
        responses.delete(f"{BOTS_URL}/bot-1", json={})
        responses.delete(f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1", json={})
        assert BotTranscriptionService().stop(room, user=admin)["status"] == "stopped"
        assert len(responses.calls) == 2

    def test_a_stranger_may_not_stop(self, run_settings):
        room = RoomFactory()
        self._running(room, str(UserFactory().id))
        with pytest.raises(PermissionDeniedError):
            BotTranscriptionService().stop(room, user=UserFactory())

    def test_idle_without_a_run(self, run_settings):
        assert BotTranscriptionService().stop(RoomFactory(), user=None) == {
            "status": "idle"
        }

    def test_a_concurrent_stop_does_nothing(self, run_settings):
        room = RoomFactory()
        starter = UserFactory()
        self._running(room, str(starter.id))
        from django.core.cache import cache  # noqa: PLC0415

        cache.add(f"linto:stop:{room.id}", "1", 60)
        assert BotTranscriptionService().stop(room, user=starter) == {
            "status": "stopping"
        }
        self.enqueue.assert_not_called()

    @responses.activate
    def test_a_studio_failure_still_closes_the_run(self, run_settings):
        room = RoomFactory()
        starter = UserFactory()
        self._running(room, str(starter.id))
        responses.delete(f"{BOTS_URL}/bot-1", status=500, body="boom")
        responses.delete(
            f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1", status=502, body="x"
        )
        assert BotTranscriptionService().stop(room, user=starter)["status"] == (
            "stopped"
        )
        room.refresh_from_db()
        assert "linto" not in room.configuration
        self.enqueue.assert_called_once_with(room)

    @responses.activate
    def test_user_key_run_is_stopped_with_the_starters_key(self, user_key_settings):
        room, admin = _owner_room()
        starter = UserFactory(sub="starter-sub")
        self._running(room, str(starter.id))
        responses.post(
            EXCHANGE,
            json={"token": "starter-jwt", "expiresIn": 3600, "organizationId": ORG},
        )
        responses.delete(f"{BOTS_URL}/bot-1", json={})
        responses.delete(f"{STUDIO}/api/organizations/{ORG}/sessions/sess-1", json={})
        BotTranscriptionService().stop(room, user=admin)
        exchange = json.loads(responses.calls[0].request.body)
        assert exchange["subject"] == "starter-sub"
        deletes = [c for c in responses.calls if c.request.method == "DELETE"]
        assert len(deletes) == 2
        assert all(
            c.request.headers["Authorization"] == "Bearer starter-jwt" for c in deletes
        )
