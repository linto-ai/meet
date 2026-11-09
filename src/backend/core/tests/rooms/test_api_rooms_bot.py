"""
Test the LinTO live-transcription HTTP endpoints (browser-first lifecycle).

The browser drives Studio via the SDK; these Meet endpoints only mint the native
bot token, gate on permissions, and drive the banner/egress/summary lifecycle.
Auth = a LiveKit room token in an `X-LiveKit-Token` Authorization header. The service layer is mocked.
"""

# pylint: disable=redefined-outer-name

from unittest import mock

from django.contrib.auth.models import AnonymousUser

import pytest
from livekit.api import AccessToken, VideoGrants
from rest_framework.test import APIClient

from core.authentication.livekit import LIVEKIT_AUTH_SCHEME
from core.factories import RoomFactory, UserFactory
from core.services.bot_transcription import (
    BotTranscriptionException,
    NoProfileError,
    PermissionDeniedError,
    RunConflictError,
    StudioNotEntitled,
    StudioRefusedError,
)
from core.utils import generate_token

pytestmark = pytest.mark.django_db

LK_KEY = "devkey"
LK_SECRET = "secret"


@pytest.fixture
def linto_settings(settings):
    settings.LINTO_FEATURE_ENABLED = True
    settings.LIVEKIT_CONFIGURATION = {
        "api_key": LK_KEY,
        "api_secret": LK_SECRET,
        "url": "ws://livekit:7880",
    }
    return settings


def _room_token(room, admin=False, user=None):
    if user is not None:
        # The token Meet mints for a user, who the LiveKit authentication names
        return generate_token(room=str(room.id), user=user)
    grants = VideoGrants(
        room=str(room.id),
        room_join=True,
        room_admin=admin,
    )
    return (
        AccessToken(api_key=LK_KEY, api_secret=LK_SECRET)
        .with_identity("user-1")
        .with_grants(grants)
        .to_jwt()
    )


def _auth(token):
    return f"{LIVEKIT_AUTH_SCHEME} {token}"


def _post(client, room, path, body=None):
    token = _room_token(room)
    return client.post(
        f"/api/v1.0/rooms/{room.id}/linto/{path}/",
        body or {},
        format="json",
        HTTP_AUTHORIZATION=_auth(token),
    )


class TestFeatureFlag:
    def test_disabled_returns_404(self, linto_settings):
        linto_settings.LINTO_FEATURE_ENABLED = False
        room = RoomFactory()
        res = _post(APIClient(), room, "prepare", {"channel_id": "c"})
        assert res.status_code == 404


class TestPrepare:
    def test_requires_channel_id(self, linto_settings):
        room = RoomFactory()
        res = _post(APIClient(), room, "prepare", {})
        assert res.status_code == 400

    def test_mints_token(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.prepare",
            return_value={
                "token": "lk",
                "livekit_url": "ws://lk",
                "room": str(room.id),
            },
        ) as prep:
            res = _post(APIClient(), room, "prepare", {"channel_id": "chan-1"})
        assert res.status_code == 200
        assert res.json()["token"] == "lk"
        prep.assert_called_once()

    def test_permission_denied_maps_to_403(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.prepare",
            side_effect=PermissionDeniedError("nope"),
        ):
            res = _post(APIClient(), room, "prepare", {"channel_id": "c"})
        assert res.status_code == 403
        assert res.json()["code"] == "permission_denied"


class TestStartedStopped:
    def test_started_ok(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.mark_started",
            return_value={"status": "running"},
        ) as started:
            res = _post(
                APIClient(),
                room,
                "started",
                {"session_id": "s", "channel_id": "c", "org_id": "o", "bot_id": "b"},
            )
        assert res.status_code == 200
        assert res.json()["status"] == "running"
        started.assert_called_once()

    def test_stopped_ok(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.mark_stopped",
            return_value={"status": "stopped"},
        ) as stopped:
            res = _post(APIClient(), room, "stopped", {"conversation_name": "linto-x"})
        assert res.status_code == 200
        assert res.json()["status"] == "stopped"
        stopped.assert_called_once()


class TestStudioToken:
    """GET linto/studio-token is the identity bridge: the caller's Meet identity
    → a Studio JWT + organization for the browser SDK, from the configured
    token source. Anonymous guests (no Meet user behind the LiveKit identity)
    have nothing to bridge."""

    def test_anonymous_is_forbidden(self, linto_settings):
        room = RoomFactory()
        token = _room_token(room)  # identity "user-1" matches no Meet user
        # A concrete answer: were the caller let through, the view serializes it
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.studio_token_for",
            return_value={"enabled": True},
        ) as service:
            res = APIClient().get(
                f"/api/v1.0/rooms/{room.id}/linto/studio-token/",
                HTTP_AUTHORIZATION=_auth(token),
            )
        assert res.status_code == 403
        assert res.json()["code"] == "anonymous"
        service.assert_not_called()

    def test_anonymous_with_the_sub_of_a_user_is_forbidden(self, linto_settings):
        """The identity of an anonymous participant comes from the client: a
        token minted for one does not bridge the user whose sub it equals."""
        UserFactory(sub="user-1")
        room = RoomFactory()
        token = generate_token(
            room=str(room.id), user=AnonymousUser(), participant_id="user-1"
        )
        # A concrete answer: were the caller let through, the view serializes it
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.studio_token_for",
            return_value={"enabled": True},
        ) as service:
            res = APIClient().get(
                f"/api/v1.0/rooms/{room.id}/linto/studio-token/",
                HTTP_AUTHORIZATION=_auth(token),
            )
        assert res.status_code == 403
        assert res.json()["code"] == "anonymous"
        service.assert_not_called()

    def test_authenticated_user_gets_the_token_context(self, linto_settings):
        user = UserFactory(sub="user-1")
        room = RoomFactory()
        token = _room_token(room, user=user)
        payload = {
            "enabled": True,
            "token": "jwt",
            "base_url": "http://studio",
            "organization_id": "org1",
            "expires_in": 3600,
            "capabilities": {"quickMeeting": True},
        }
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.studio_token_for",
            return_value=payload,
        ) as service:
            res = APIClient().get(
                f"/api/v1.0/rooms/{room.id}/linto/studio-token/",
                HTTP_AUTHORIZATION=_auth(token),
            )
        assert res.status_code == 200
        assert res.json() == payload
        assert service.call_args.args[0] == user

    def test_not_entitled_is_a_200_with_enabled_false(self, linto_settings):
        user = UserFactory(sub="user-1")
        room = RoomFactory()
        token = _room_token(room, user=user)
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.studio_token_for",
            return_value={"enabled": False, "reason": "no_entitlement"},
        ):
            res = APIClient().get(
                f"/api/v1.0/rooms/{room.id}/linto/studio-token/",
                HTTP_AUTHORIZATION=_auth(token),
            )
        assert res.status_code == 200
        assert res.json() == {"enabled": False, "reason": "no_entitlement"}

    def test_studio_failure_is_a_502(self, linto_settings):
        user = UserFactory(sub="user-1")
        room = RoomFactory()
        token = _room_token(room, user=user)
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.studio_token_for",
            side_effect=BotTranscriptionException("no Studio credentials"),
        ):
            res = APIClient().get(
                f"/api/v1.0/rooms/{room.id}/linto/studio-token/",
                HTTP_AUTHORIZATION=_auth(token),
            )
        assert res.status_code == 502
        assert "credentials" in res.json()["error"]


class TestSummaryServices:
    def test_lists_the_services_for_a_room_participant(self, linto_settings):
        room = RoomFactory()
        token = _room_token(room)
        services = [
            {
                "route": "minutes",
                "name": "Minutes",
                "description": {},
                "icon": None,
                "default": True,
            }
        ]
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.summary_services",
            return_value=services,
        ):
            res = APIClient().get(
                f"/api/v1.0/rooms/{room.id}/linto/summary-services/",
                HTTP_AUTHORIZATION=_auth(token),
            )
        assert res.status_code == 200
        assert res.json() == {"services": services}

    def test_feature_off_is_a_404(self, linto_settings):
        linto_settings.LINTO_FEATURE_ENABLED = False
        room = RoomFactory()
        token = _room_token(room)
        res = APIClient().get(
            f"/api/v1.0/rooms/{room.id}/linto/summary-services/",
            HTTP_AUTHORIZATION=_auth(token),
        )
        assert res.status_code == 404


class TestTranscriptionLanguages:
    def test_lists_the_languages_for_a_room_participant(self, linto_settings):
        room = RoomFactory()
        token = _room_token(room)
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.transcription_languages",
            return_value=["*", "en", "fr"],
        ):
            res = APIClient().get(
                f"/api/v1.0/rooms/{room.id}/linto/transcription-languages/",
                HTTP_AUTHORIZATION=_auth(token),
            )
        assert res.status_code == 200
        assert res.json() == {"languages": ["*", "en", "fr"]}

    def test_feature_off_is_a_404(self, linto_settings):
        linto_settings.LINTO_FEATURE_ENABLED = False
        room = RoomFactory()
        token = _room_token(room)
        res = APIClient().get(
            f"/api/v1.0/rooms/{room.id}/linto/transcription-languages/",
            HTTP_AUTHORIZATION=_auth(token),
        )
        assert res.status_code == 404


class TestAuth:
    def test_no_token_is_unauthorized(self, linto_settings):
        room = RoomFactory()
        res = APIClient().post(
            f"/api/v1.0/rooms/{room.id}/linto/prepare/",
            {"channel_id": "c"},
            format="json",
        )
        assert res.status_code in (401, 403)

    def test_a_token_outside_the_authorization_header_is_ignored(self, linto_settings):
        """The room token travels in the header only, never in a URL or a
        body, as upstream's LiveKit token authentication wants it."""
        room = RoomFactory()
        token = _room_token(room)
        res = APIClient().post(
            f"/api/v1.0/rooms/{room.id}/linto/prepare/?token={token}",
            {"channel_id": "c", "token": token},
            format="json",
        )
        assert res.status_code in (401, 403)

    def test_a_user_access_token_does_not_hide_the_room_token(self, linto_settings):
        """Embedded mode: the frontend sends the room token in the Authorization
        header in place of the user access token (Bearer) fetchApi would set."""
        room = RoomFactory()
        res = APIClient().post(
            f"/api/v1.0/rooms/{room.id}/linto/prepare/",
            {"channel_id": "c"},
            format="json",
            HTTP_AUTHORIZATION="Bearer some-user-access-token",
        )
        assert res.status_code in (401, 403)
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.prepare",
            return_value={"token": "lk", "livekit_url": "ws://lk", "room": "r"},
        ):
            res = _post(APIClient(), room, "prepare", {"channel_id": "c"})
        assert res.status_code == 200


class TestProfiles:
    def _get(self, room, token):
        return APIClient().get(
            f"/api/v1.0/rooms/{room.id}/linto/profiles/",
            HTTP_AUTHORIZATION=_auth(token),
        )

    def test_anonymous_is_forbidden(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.quick_meeting_profiles"
        ) as service:
            res = self._get(room, _room_token(room))
        assert res.status_code == 403
        assert res.json()["code"] == "anonymous"
        service.assert_not_called()

    def test_lists_the_profiles(self, linto_settings):
        user = UserFactory(sub="user-1")
        room = RoomFactory()
        profiles = [{"id": "220", "name": "FR", "languages": [], "translations": []}]
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.quick_meeting_profiles",
            return_value=profiles,
        ) as service:
            res = self._get(room, _room_token(room, user=user))
        assert res.status_code == 200
        assert res.json() == {"profiles": profiles, "reason": "ok"}
        assert service.call_args.args[0] == user

    def test_an_organization_without_profile_is_unprovisioned(self, linto_settings):
        user = UserFactory(sub="user-1")
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.quick_meeting_profiles",
            return_value=[],
        ):
            res = self._get(room, _room_token(room, user=user))
        assert res.json() == {"profiles": [], "reason": "unprovisioned"}

    def test_not_entitled_is_a_200(self, linto_settings):
        user = UserFactory(sub="user-1")
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.quick_meeting_profiles",
            side_effect=StudioNotEntitled("revoked"),
        ):
            res = self._get(room, _room_token(room, user=user))
        assert res.status_code == 200
        assert res.json() == {
            "profiles": [],
            "reason": "no_entitlement",
            "detail": "revoked",
        }

    def test_studio_failure_is_a_502(self, linto_settings):
        user = UserFactory(sub="user-1")
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.quick_meeting_profiles",
            side_effect=BotTranscriptionException("Studio returned 500: secret"),
        ):
            res = self._get(room, _room_token(room, user=user))
        assert res.status_code == 502
        assert "secret" not in res.content.decode()


class TestStart:
    def test_starts_the_run(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.start",
            return_value={"status": "running", "session_id": "s"},
        ) as start:
            res = APIClient().post(
                f"/api/v1.0/rooms/{room.id}/linto/start/",
                {"translations": ["en"], "summary": True},
                format="json",
                HTTP_AUTHORIZATION=_auth(_room_token(room)),
                HTTP_ORIGIN="https://meet.example",
            )
        assert res.status_code == 200
        assert res.json() == {"status": "running", "session_id": "s"}
        assert start.call_args.args[1] == {"translations": ["en"], "summary": True}
        assert start.call_args.kwargs["origin"] == "https://meet.example"

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (PermissionDeniedError("no"), 403, "permission_denied"),
            (StudioNotEntitled("revoked"), 403, "not_entitled"),
            (RunConflictError("already_running"), 409, "already_running"),
            (RunConflictError("already_starting"), 409, "already_starting"),
            (NoProfileError("none"), 409, "no_profile"),
        ],
    )
    def test_refusals_carry_a_code(self, linto_settings, error, status, code):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.start", side_effect=error
        ):
            res = _post(APIClient(), room, "start")
        assert res.status_code == status
        assert res.json()["code"] == code

    def test_a_studio_refusal_is_a_403_the_panel_can_explain(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.start",
            side_effect=StudioRefusedError("session creation", 402, "plan"),
        ):
            res = _post(APIClient(), room, "start")
        assert res.status_code == 403
        assert res.json() == {
            "error": "LinTO refused to start the live transcription",
            "code": "studio_refused",
            "studio_status": 402,
            "studio_code": "plan",
        }

    def test_a_studio_failure_is_a_502_without_its_details(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.start",
            side_effect=BotTranscriptionException("Studio returned 500: internals"),
        ):
            res = _post(APIClient(), room, "start")
        assert res.status_code == 502
        assert "internals" not in res.content.decode()


class TestStop:
    def test_stops_the_run(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.stop",
            return_value={"status": "stopped"},
        ) as stop:
            res = _post(APIClient(), room, "stop")
        assert res.status_code == 200
        assert res.json() == {"status": "stopped"}
        stop.assert_called_once()

    def test_a_stranger_is_forbidden(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.stop",
            side_effect=PermissionDeniedError("no"),
        ):
            res = _post(APIClient(), room, "stop")
        assert res.status_code == 403
        assert res.json()["code"] == "permission_denied"
