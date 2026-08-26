"""
Test rooms API endpoints in the Meet core app: the LinTO transcription-bot
actions (start-bot, stop-bot, bot-status, bot-profiles).

These 4 actions use ``LiveKitTokenAuthentication`` + ``HasLiveKitRoomAccess``
(same as start-subtitle). The BotTranscriptionService is mocked so no Studio
HTTP is performed here — the service itself is covered in
``core/tests/services/test_bot_transcription.py``.
"""

# pylint: disable=redefined-outer-name,unused-argument

import uuid
from unittest import mock

from django.conf import settings

import pytest
import requests
from livekit.api import AccessToken, VideoGrants
from rest_framework.test import APIClient

from core.services.bot_transcription import (
    BotTranscriptionException,
    NoQuickMeetingProfile,
    PermissionDeniedError,
)

from ...factories import RoomFactory, UserFactory

pytestmark = pytest.mark.django_db

MOCK_ROOM_ID = "d2aeb774-1ecd-4d73-a3ac-3d3530cad7ff"


@pytest.fixture
def mock_livekit_token():
    """A valid LiveKit JWT granting access to MOCK_ROOM_ID."""
    video_grants = VideoGrants(
        room=MOCK_ROOM_ID,
        room_join=True,
        room_admin=True,
        can_update_own_metadata=True,
    )
    token = (
        AccessToken(
            api_key=settings.LIVEKIT_CONFIGURATION["api_key"],
            api_secret=settings.LIVEKIT_CONFIGURATION["api_secret"],
        )
        .with_grants(video_grants)
        .with_identity(str(uuid.uuid4()))
    )
    return token.to_jwt()


@pytest.fixture
def mock_service():
    """Patch the BotTranscriptionService class used by the viewset."""
    with mock.patch("core.api.viewsets.BotTranscriptionService") as service_cls:
        instance = service_cls.return_value
        yield instance


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


# ── auth / permission (shared shape across the actions) ──────────────────────
class TestBotAuth:
    """Auth + room-access gating, exercised through start-bot / bot-status."""

    def test_missing_token_anonymous(self, settings):
        """No LiveKit token → 403 (credentials not provided)."""
        settings.LINTO_FEATURE_ENABLED = True
        room = RoomFactory()
        response = APIClient().post(f"/api/v1.0/rooms/{room.id}/start-bot/")
        assert response.status_code == 403
        assert response.json() == {
            "detail": "Authentication credentials were not provided."
        }

    def test_invalid_token(self, settings):
        """A malformed LiveKit token → 403 invalid token."""
        settings.LINTO_FEATURE_ENABLED = True
        room = RoomFactory()
        response = APIClient().post(
            f"/api/v1.0/rooms/{room.id}/start-bot/", {}, **_auth("garbage")
        )
        assert response.status_code == 403
        assert response.json() == {
            "detail": "Invalid LiveKit token: Not enough segments"
        }

    def test_wrong_room(self, settings, mock_livekit_token):
        """A token minted for another room → 403 permission denied."""
        settings.LINTO_FEATURE_ENABLED = True
        room = RoomFactory()  # id != MOCK_ROOM_ID
        response = APIClient().get(
            f"/api/v1.0/rooms/{room.id}/bot-status/", **_auth(mock_livekit_token)
        )
        assert response.status_code == 403
        assert response.json() == {
            "detail": "You do not have permission to perform this action."
        }


# ── start-bot ────────────────────────────────────────────────────────────────
class TestStartBot:
    """start-bot: feature flag, happy path, and service-exception → HTTP mapping."""

    def test_feature_disabled_returns_404(self, settings, mock_livekit_token):
        """LINTO_FEATURE_ENABLED off → 404, service never touched."""
        settings.LINTO_FEATURE_ENABLED = False
        RoomFactory(id=MOCK_ROOM_ID)
        response = APIClient().post(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/start-bot/",
            {},
            **_auth(mock_livekit_token),
        )
        assert response.status_code == 404

    def test_happy_path(self, settings, mock_livekit_token, mock_service):
        """Valid token + enabled → 200 with the service payload; config passed through."""
        settings.LINTO_FEATURE_ENABLED = True
        mock_service.start_bot.return_value = {"status": "running", "session_id": "s1"}
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().post(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/start-bot/",
            {"summary": True, "record": False},
            **_auth(mock_livekit_token),
        )
        assert response.status_code == 200
        assert response.json() == {"status": "running", "session_id": "s1"}
        mock_service.start_bot.assert_called_once()
        _, kwargs = mock_service.start_bot.call_args
        # Serializer validated data reaches the service (defaults applied).
        assert mock_service.start_bot.call_args.args[1]["summary"] is True

    def test_no_quickmeeting_profile_returns_409(
        self, settings, mock_livekit_token, mock_service
    ):
        """NoQuickMeetingProfile → 409 with the machine code."""
        settings.LINTO_FEATURE_ENABLED = True
        mock_service.start_bot.side_effect = NoQuickMeetingProfile("no profile")
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().post(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/start-bot/",
            {},
            **_auth(mock_livekit_token),
        )
        assert response.status_code == 409
        assert response.json()["code"] == "no_quickmeeting_profile"

    def test_permission_denied_returns_403(
        self, settings, mock_livekit_token, mock_service
    ):
        """PermissionDeniedError → 403 with code permission_denied."""
        settings.LINTO_FEATURE_ENABLED = True
        mock_service.start_bot.side_effect = PermissionDeniedError("denied")
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().post(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/start-bot/",
            {},
            **_auth(mock_livekit_token),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "permission_denied"

    def test_generic_exception_returns_502(
        self, settings, mock_livekit_token, mock_service
    ):
        """A generic BotTranscriptionException → 502."""
        settings.LINTO_FEATURE_ENABLED = True
        mock_service.start_bot.side_effect = BotTranscriptionException("upstream boom")
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().post(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/start-bot/",
            {},
            **_auth(mock_livekit_token),
        )
        assert response.status_code == 502


# ── stop-bot ─────────────────────────────────────────────────────────────────
class TestStopBot:
    """stop-bot: happy path + permission/generic error mapping (no feature flag gate)."""

    def test_happy_path(self, mock_livekit_token, mock_service):
        """Valid token → 200 with the service payload."""
        mock_service.stop_bot.return_value = {"status": "stopped"}
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().post(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/stop-bot/", {}, **_auth(mock_livekit_token)
        )
        assert response.status_code == 200
        assert response.json() == {"status": "stopped"}

    def test_permission_denied_returns_403(self, mock_livekit_token, mock_service):
        """Only the starter/admin may stop → PermissionDeniedError maps to 403."""
        mock_service.stop_bot.side_effect = PermissionDeniedError("nope")
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().post(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/stop-bot/", {}, **_auth(mock_livekit_token)
        )
        assert response.status_code == 403
        assert response.json()["code"] == "permission_denied"

    def test_generic_exception_returns_502(self, mock_livekit_token, mock_service):
        """A generic BotTranscriptionException → 502."""
        mock_service.stop_bot.side_effect = BotTranscriptionException("boom")
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().post(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/stop-bot/", {}, **_auth(mock_livekit_token)
        )
        assert response.status_code == 502


# ── bot-status ───────────────────────────────────────────────────────────────
class TestBotStatus:
    """bot-status: happy path + generic error mapping."""

    def test_happy_path(self, mock_livekit_token, mock_service):
        """Valid token → 200 with status + captions payload."""
        mock_service.bot_status.return_value = {"status": "idle", "captions": []}
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().get(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/bot-status/", **_auth(mock_livekit_token)
        )
        assert response.status_code == 200
        assert response.json() == {"status": "idle", "captions": []}

    def test_generic_exception_returns_502(self, mock_livekit_token, mock_service):
        """A generic BotTranscriptionException → 502."""
        mock_service.bot_status.side_effect = BotTranscriptionException("boom")
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().get(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/bot-status/", **_auth(mock_livekit_token)
        )
        assert response.status_code == 502


# ── bot-profiles ─────────────────────────────────────────────────────────────
class TestBotProfiles:
    """bot-profiles: provisioned / unprovisioned / upstream-degraded / auth-failure."""

    def test_provisioned(self, settings, mock_livekit_token, mock_service):
        """A non-empty profile list → 200 reason=ok, hasDefault true."""
        settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = ""
        mock_service.list_profiles_for_room.return_value = [{"id": "p-1", "name": "N"}]
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().get(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/bot-profiles/", **_auth(mock_livekit_token)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["reason"] == "ok"
        assert body["hasDefault"] is True
        assert body["profiles"] == [{"id": "p-1", "name": "N"}]

    def test_unprovisioned(self, settings, mock_livekit_token, mock_service):
        """An empty profile list with no default → 200 reason=unprovisioned."""
        settings.LINTO_STUDIO_DEFAULT_PROFILE_ID = ""
        mock_service.list_profiles_for_room.return_value = []
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().get(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/bot-profiles/", **_auth(mock_livekit_token)
        )
        assert response.status_code == 200
        assert response.json()["reason"] == "unprovisioned"

    def test_upstream_error_degrades_to_200(self, mock_livekit_token, mock_service):
        """A transient network error → 200 reason=upstream_error (not a failure)."""
        mock_service.list_profiles_for_room.side_effect = requests.ConnectionError()
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().get(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/bot-profiles/", **_auth(mock_livekit_token)
        )
        assert response.status_code == 200
        assert response.json()["reason"] == "upstream_error"

    def test_auth_failure_returns_502(self, mock_livekit_token, mock_service):
        """A real credentials failure (BotTranscriptionException) → 502."""
        mock_service.list_profiles_for_room.side_effect = BotTranscriptionException(
            "no token"
        )
        RoomFactory(id=MOCK_ROOM_ID)

        response = APIClient().get(
            f"/api/v1.0/rooms/{MOCK_ROOM_ID}/bot-profiles/", **_auth(mock_livekit_token)
        )
        assert response.status_code == 502
