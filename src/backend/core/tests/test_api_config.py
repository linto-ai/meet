"""
Test config API endpoint in the Meet core app.
"""

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_config_exposes_recording_permissions(settings):
    """Config endpoint should expose recording permission levels."""
    settings.RECORDING_ENABLE = True
    settings.RECORDING_SCREEN_PERMISSION = "authenticated"
    settings.RECORDING_TRANSCRIPT_PERMISSION = "admin_owner"

    client = APIClient()
    response = client.get("/api/v1.0/config/")

    assert response.status_code == 200
    data = response.json()
    assert data["recording"]["screen_recording_permission"] == "authenticated"
    assert data["recording"]["transcript_permission"] == "admin_owner"


def test_config_recording_permissions_default_values(settings):
    """Config endpoint should return default permission values."""
    settings.RECORDING_ENABLE = True
    settings.RECORDING_SCREEN_PERMISSION = "admin_owner"
    settings.RECORDING_TRANSCRIPT_PERMISSION = "admin_owner"

    client = APIClient()
    response = client.get("/api/v1.0/config/")

    assert response.status_code == 200
    data = response.json()
    assert data["recording"]["screen_recording_permission"] == "admin_owner"
    assert data["recording"]["transcript_permission"] == "admin_owner"


def test_config_linto_reports_the_kill_switch_and_effective_token_source(settings):
    """LINTO_ENTITLEMENTS_ENABLED is exposed, and `token_source` is the one the
    bridge really uses: the service account once the gating is off."""
    settings.LINTO_FEATURE_ENABLED = True
    settings.LINTO_STUDIO_TOKEN_SOURCE = "user_key"
    settings.LINTO_ENTITLEMENTS_ENABLED = True
    data = APIClient().get("/api/v1.0/config/").json()
    assert data["linto"]["entitlements_enabled"] is True
    assert data["linto"]["token_source"] == "user_key"

    settings.LINTO_ENTITLEMENTS_ENABLED = False
    data = APIClient().get("/api/v1.0/config/").json()
    assert data["linto"]["entitlements_enabled"] is False
    assert data["linto"]["token_source"] == "service_account"


def test_config_linto_carries_no_studio_credential_nor_bot_wiring(settings):
    settings.LINTO_FEATURE_ENABLED = True
    settings.LINTO_STUDIO_API_TOKEN = "the-studio-key"
    settings.LINTO_NATIVE_LIVEKIT_URL = "ws://livekit.internal:7880"
    response = APIClient().get("/api/v1.0/config/")
    linto = response.json()["linto"]
    for key in (
        "native_livekit_url",
        "visio_native_enabled",
        "bot_provider",
        "live_keep_audio",
    ):
        assert key not in linto
    assert "the-studio-key" not in response.content.decode()
    assert "livekit.internal" not in response.content.decode()
