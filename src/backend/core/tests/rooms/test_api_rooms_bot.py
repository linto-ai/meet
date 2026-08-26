"""
Test the LinTO live-transcription HTTP endpoints (browser-first lifecycle).

The browser drives Studio via the SDK; these Meet endpoints only mint the native
bot token, gate on permissions, and drive the banner/egress/summary lifecycle.
Auth = a LiveKit room token (header/body/query). The service layer is mocked.
"""

# pylint: disable=redefined-outer-name

from unittest import mock

import pytest
from livekit.api import AccessToken, VideoGrants
from rest_framework.test import APIClient

from core.factories import RoomFactory
from core.services.bot_transcription import PermissionDeniedError

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


def _room_token(room, admin=False):
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


def _post(client, room, path, body=None):
    token = _room_token(room)
    payload = {"token": token, **(body or {})}
    return client.post(
        f"/api/v1.0/rooms/{room.id}/linto/{path}/", payload, format="json"
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


class TestDevStudioToken:
    def test_disabled_returns_404(self, linto_settings):
        linto_settings.LINTO_STUDIO_DEV_TOKEN_ENABLED = False
        room = RoomFactory()
        token = _room_token(room)
        res = APIClient().get(
            f"/api/v1.0/rooms/{room.id}/linto/studio-token/?token={token}"
        )
        assert res.status_code == 404

    def test_enabled_returns_token(self, linto_settings):
        linto_settings.LINTO_STUDIO_DEV_TOKEN_ENABLED = True
        room = RoomFactory()
        token = _room_token(room)
        with mock.patch(
            "core.api.viewsets.BotTranscriptionService.dev_studio_token",
            return_value={"token": "jwt", "base_url": "http://studio"},
        ):
            res = APIClient().get(
                f"/api/v1.0/rooms/{room.id}/linto/studio-token/?token={token}"
            )
        assert res.status_code == 200
        assert res.json() == {"token": "jwt", "base_url": "http://studio"}


class TestAuth:
    def test_no_token_is_unauthorized(self, linto_settings):
        room = RoomFactory()
        res = APIClient().post(
            f"/api/v1.0/rooms/{room.id}/linto/prepare/",
            {"channel_id": "c"},
            format="json",
        )
        assert res.status_code in (401, 403)
