"""
Test the LinTO live-transcription HTTP endpoints.

Auth = a LiveKit room token in an `X-LiveKit-Token` Authorization header. The service layer is mocked.
"""

# pylint: disable=redefined-outer-name

from unittest import mock

from django.contrib.auth.models import AnonymousUser

import pytest
import responses
from livekit.api import AccessToken, VideoGrants
from rest_framework.test import APIClient

from core import models
from core.authentication.livekit import LIVEKIT_AUTH_SCHEME
from core.factories import RoomFactory, UserFactory
from core.services.bot_transcription import (
    BotTranscriptionException,
    BotTranscriptionService,
    NoProfileError,
    PermissionDeniedError,
    RunConflictError,
    StudioNotEntitled,
    StudioRefusedError,
    TooManyTranslationsError,
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
    @pytest.mark.parametrize("path", ["start", "stop"])
    def test_disabled_returns_404(self, linto_settings, path):
        linto_settings.LINTO_FEATURE_ENABLED = False
        room = RoomFactory()
        res = _post(APIClient(), room, path)
        assert res.status_code == 404


class TestBrowserPathRemoved:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "studio-token"),
            ("post", "prepare"),
            ("post", "started"),
            ("post", "stopped"),
        ],
    )
    def test_the_route_is_gone(self, linto_settings, method, path):
        user = UserFactory(sub="user-1")
        room = RoomFactory()
        res = getattr(APIClient(), method)(
            f"/api/v1.0/rooms/{room.id}/linto/{path}/",
            {},
            format="json",
            HTTP_AUTHORIZATION=_auth(_room_token(room, user=user)),
        )
        assert res.status_code in (404, 405)


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
            f"/api/v1.0/rooms/{room.id}/linto/start/",
            {},
            format="json",
        )
        assert res.status_code in (401, 403)

    def test_a_token_outside_the_authorization_header_is_ignored(self, linto_settings):
        """The room token travels in the header only, never in a URL or a
        body, as upstream's LiveKit token authentication wants it."""
        room = RoomFactory()
        token = _room_token(room)
        res = APIClient().post(
            f"/api/v1.0/rooms/{room.id}/linto/start/?token={token}",
            {"token": token},
            format="json",
        )
        assert res.status_code in (401, 403)

    def test_a_user_access_token_does_not_hide_the_room_token(self, linto_settings):
        """Embedded mode: the frontend sends the room token in the Authorization
        header in place of the user access token (Bearer) fetchApi would set."""
        room = RoomFactory()
        res = APIClient().post(
            f"/api/v1.0/rooms/{room.id}/linto/start/",
            {},
            format="json",
            HTTP_AUTHORIZATION="Bearer some-user-access-token",
        )
        assert res.status_code in (401, 403)
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.start",
            return_value={"status": "running"},
        ):
            res = _post(APIClient(), room, "start")
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

    def test_anonymous_with_the_sub_of_a_user_is_forbidden(self, linto_settings):
        UserFactory(sub="user-1")
        room = RoomFactory()
        token = generate_token(
            room=str(room.id), user=AnonymousUser(), participant_id="user-1"
        )
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.quick_meeting_profiles"
        ) as service:
            res = self._get(room, token)
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
            (TooManyTranslationsError(2), 400, "too_many_translations"),
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

    def test_too_many_translations_carries_the_limit(self, linto_settings):
        room = RoomFactory()
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.start",
            side_effect=TooManyTranslationsError(3),
        ):
            res = _post(APIClient(), room, "start")
        assert res.status_code == 400
        assert res.json()["max_translations"] == 3

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


STUDIO = "http://studio.test"
STUDIO_KEY = "eyJ-the-studio-key"
USER_KEY_TOKEN = "eyJ-the-users-own-key-token"


@responses.activate
@pytest.mark.parametrize("source", ["service_account", "user_key"])
def test_no_answer_to_the_browser_carries_a_studio_credential(linto_settings, source):
    org = "org-1"
    linto_settings.LINTO_STUDIO_BASE_URL = STUDIO
    linto_settings.LINTO_STUDIO_BROWSER_API_URL = "http://studio.browser"
    linto_settings.LINTO_STUDIO_DEFAULT_ORG_ID = org
    linto_settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = ""
    linto_settings.LINTO_NATIVE_LIVEKIT_URL = "ws://livekit:7880"
    linto_settings.EMAIL_APP_BASE_URL = "https://meet.example"
    linto_settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    if source == "service_account":
        linto_settings.LINTO_ENTITLEMENTS_ENABLED = False
        linto_settings.LINTO_STUDIO_API_TOKEN = STUDIO_KEY
    else:
        linto_settings.LINTO_ENTITLEMENTS_ENABLED = True
        linto_settings.LINTO_STUDIO_TOKEN_SOURCE = "user_key"
        linto_settings.LINTO_STUDIO_INTEGRATION_TOKEN = STUDIO_KEY
        responses.post(
            f"{STUDIO}/api/auth/external/token",
            json={"token": USER_KEY_TOKEN, "expiresIn": 3600, "organizationId": org},
        )
    from django.core.cache import cache  # noqa: PLC0415

    cache.clear()
    base = f"{STUDIO}/api/organizations/{org}"
    responses.get(
        f"{base}/transcriber_profiles",
        json=[{"id": 220, "name": "FR", "config": {}, "translations": ["en"]}],
    )
    responses.post(f"{base}/sessions/", json={"id": "sess-1", "channels": [{"id": 7}]})
    responses.patch(f"{base}/sessions/sess-1", json={})
    responses.post(f"{base}/bots", json={"id": "bot-1"})
    responses.delete(f"{base}/bots/bot-1", json={})
    responses.delete(f"{base}/sessions/sess-1", json={})

    user = UserFactory(sub="user-1")
    room = RoomFactory()
    room.accesses.create(user=user, role=models.RoleChoices.OWNER)
    auth = _auth(_room_token(room, user=user))
    client = APIClient()
    with (
        mock.patch("core.services.bot_transcription.RoomManagement"),
        mock.patch.object(BotTranscriptionService, "_stop_native_subtitles"),
        mock.patch.object(BotTranscriptionService, "_enqueue_summary"),
        mock.patch(
            "core.services.bot_transcription.summary_entitled", return_value=True
        ),
    ):
        answers = [
            client.get("/api/v1.0/config/"),
            client.get(
                f"/api/v1.0/rooms/{room.id}/linto/profiles/",
                HTTP_AUTHORIZATION=auth,
            ),
            client.post(
                f"/api/v1.0/rooms/{room.id}/linto/start/",
                {"translations": ["en"]},
                format="json",
                HTTP_AUTHORIZATION=auth,
            ),
        ]
        client.force_login(user)
        answers.append(client.get(f"/api/v1.0/rooms/{room.id}/"))
        client.logout()
        answers.append(
            client.post(
                f"/api/v1.0/rooms/{room.id}/linto/stop/",
                format="json",
                HTTP_AUTHORIZATION=auth,
            )
        )

    assert [answer.status_code for answer in answers] == [200] * 5
    assert answers[1].json()["profiles"][0]["id"] == "220"
    assert answers[2].json()["session_id"] == "sess-1"
    assert answers[4].json() == {"status": "stopped"}
    studio_calls = [c for c in responses.calls if "/organizations/" in c.request.url]
    expected = USER_KEY_TOKEN if source == "user_key" else STUDIO_KEY
    assert {c.request.headers["Authorization"] for c in studio_calls} == {
        f"Bearer {expected}"
    }
    for answer in answers:
        body = answer.content.decode()
        assert STUDIO_KEY not in body
        assert USER_KEY_TOKEN not in body
