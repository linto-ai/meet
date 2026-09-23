"""
Test the LinTO live-transcription lifecycle service (browser-first).

The BROWSER drives Studio via the JS SDK; this service only mints the native bot
join token, gates on permissions, drives the banner/egress/summary lifecycle, and
(teardown/summary) talks to Studio under the service account. Studio HTTP is
mocked; LiveKit side effects (banner metadata, native-agent stop, token mint,
egress) are patched.
"""

# pylint: disable=redefined-outer-name,protected-access

import base64
import json
from datetime import datetime, timezone
from unittest import mock

import pytest
import responses

from core import models
from core.factories import RoomFactory, UserFactory
from core.services.bot_transcription import (
    LINTO_CHANNEL_INDEX,
    ROOM_METADATA_CATCHUP_KEY,
    ROOM_METADATA_CHANNEL_ID_KEY,
    ROOM_METADATA_CHANNEL_INDEX_KEY,
    ROOM_METADATA_KEYS,
    ROOM_METADATA_ORG_ID_KEY,
    ROOM_METADATA_SESSION_ID_KEY,
    ROOM_METADATA_STARTED_AT_KEY,
    ROOM_METADATA_STARTER_KEY,
    ROOM_METADATA_STATUS_KEY,
    BotTranscriptionException,
    BotTranscriptionService,
    PermissionDeniedError,
    effective_token_source,
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
        assert linto["summary_service"] is None  # no choice → instance default
        assert linto["catchup"] is True  # late-joiner summary on by default
        assert "recording_id" not in linto  # record was False → no egress
        # The banner call carries the run state, so the metadata can advertise
        # the Studio ids a late joiner needs to catch up.
        self.banner.assert_called_once_with(room, True, user=owner, linto=linto)
        self.rec.assert_not_called()

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


class TestBannerMetadata:
    """_set_banner: the room-metadata contract read by EVERY participant.

    Beyond the status/starter pair, an active run advertises the Studio session
    identity (+ start timestamp) so a LATE JOINER's browser can fetch the
    transcript so far and the "before you arrived" summary. Everything is
    cleared as one block at stop/teardown.
    """

    def test_active_publishes_the_session_identity(self, studio_settings):
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
        }

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
                "summary_service": "minutes",
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
        assert summary["summary_service"] == "minutes"
        self.enqueue.assert_called_once_with(room)

    def test_admin_stopping_others_run_deletes_studio_session(self, studio_settings):
        # An admin who did NOT start the run has no browser-side SDK session, so
        # mark_stopped must service-account-delete the Studio bot + quick session.
        room = RoomFactory()
        admin = UserFactory()
        room.accesses.create(user=admin, role=models.RoleChoices.OWNER)
        room.configuration = {
            "linto": {
                "user_id": "someone-else",
                "org_id": "org-1",
                "session_id": "sess-1",
                "bot_id": "bot-1",
                "summary": False,
            }
        }
        room.save()
        with mock.patch.object(
            BotTranscriptionService, "_delete_studio_session"
        ) as delete:
            BotTranscriptionService().mark_stopped(room, {}, user=admin)
        delete.assert_called_once()

    def test_starter_stop_does_not_service_account_delete(self, studio_settings):
        room = RoomFactory()
        starter = UserFactory()
        room.configuration = {
            "linto": {
                "user_id": str(starter.id),
                "org_id": "org-1",
                "session_id": "sess-1",
                "bot_id": "bot-1",
                "summary": False,
            }
        }
        room.save()
        with mock.patch.object(
            BotTranscriptionService, "_delete_studio_session"
        ) as delete:
            BotTranscriptionService().mark_stopped(room, {}, user=starter)
        delete.assert_not_called()


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

    @responses.activate
    def test_clears_the_room_metadata(self, studio_settings):
        # A room ending with a run still active must leave no stale session id
        # behind, or a later joiner would try to catch up on a dead session.
        room = RoomFactory()
        room.configuration = {
            "linto": {"org_id": "org-1", "session_id": "sess-1", "summary": False}
        }
        room.save()
        responses.delete(
            f"{STUDIO}/api/organizations/org-1/quickMeeting/sess-1",
            json={"success": True},
            status=200,
        )
        with mock.patch(
            "core.services.bot_transcription.RoomManagement"
        ) as room_management:
            BotTranscriptionService().teardown(room)
        args = room_management.return_value.update_metadata.call_args[0]
        assert args[1] == {}
        assert sorted(args[2]) == sorted(ROOM_METADATA_KEYS)


def _jwt_with_exp(exp):
    """An unsigned JWT-shaped string whose payload carries ``exp``."""

    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=")

    return f"{b64({'alg': 'HS256'}).decode()}.{b64({'exp': exp}).decode()}.sig"


class TestStudioTokenFor:
    """The identity bridge for the browser SDK (LINTO_STUDIO_TOKEN_SOURCE)."""

    def test_service_account_with_pinned_org(self, studio_settings):
        studio_settings.LINTO_STUDIO_TOKEN_SOURCE = "service_account"
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org-pin"
        result = BotTranscriptionService().studio_token_for(UserFactory())
        assert result == {
            "enabled": True,
            "token": "static-token",
            "base_url": "http://studio.browser",
            "organization_id": "org-pin",
            # A static (non-JWT) token has no readable expiry.
            "expires_in": None,
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

    def test_expires_in_is_read_from_the_jwt(self, studio_settings):
        studio_settings.LINTO_STUDIO_DEFAULT_ORG_ID = "org-pin"
        studio_settings.LINTO_STUDIO_API_TOKEN = _jwt_with_exp(
            int(datetime.now(timezone.utc).timestamp()) + 3600
        )
        result = BotTranscriptionService().studio_token_for(UserFactory())
        assert 3500 < result["expires_in"] <= 3600

    def test_raises_without_credentials(self, studio_settings):
        studio_settings.LINTO_STUDIO_API_TOKEN = None
        with pytest.raises(BotTranscriptionException):
            BotTranscriptionService().studio_token_for(UserFactory())

    def test_unknown_source_raises(self, studio_settings):
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
            "base_url": "http://studio.browser",
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
