"""Tests of the fork's LinTO capabilities: the `linto` entitlements backend,
the capabilities helper, what users/me carries, and the server gate of the
deferred transcription."""

# pylint: disable=redefined-outer-name,unused-argument

import json
from unittest import mock

from django.core.cache import cache as django_cache

import pytest
import responses
from rest_framework.test import APIClient

from core import factories
from core.entitlements.backends.linto import LintoEntitlementsBackend
from core.entitlements.capabilities import (
    all_capabilities,
    has_linto_capability,
    no_capabilities,
    normalize_capabilities,
    user_linto_capabilities,
)
from core.entitlements.factory import get_entitlements_backend

pytestmark = pytest.mark.django_db

STUDIO = "http://studio.test"
RESOLVE = f"{STUDIO}/api/auth/external/resolve"
LINTO_BACKEND = "core.entitlements.backends.linto.LintoEntitlementsBackend"
LIVE_ONLY = {"transcription": {"live": True, "async": False}, "quickMeeting": True}


@pytest.fixture(autouse=True)
def _clear_caches():
    django_cache.clear()
    get_entitlements_backend.cache_clear()
    yield
    get_entitlements_backend.cache_clear()


@pytest.fixture
def linto_settings(settings):
    settings.LINTO_FEATURE_ENABLED = True
    settings.LINTO_STUDIO_ENABLED = True
    settings.LINTO_STUDIO_BASE_URL = STUDIO
    settings.LINTO_STUDIO_INTEGRATION_TOKEN = "integration-key"
    settings.LINTO_STUDIO_AUTH_EMAIL = ""
    settings.LINTO_STUDIO_AUTH_PASSWORD = ""
    settings.LINTO_IDENTITY_PROVIDER = "meet:test"
    settings.LINTO_ENTITLEMENTS_ENABLED = True
    settings.ENTITLEMENTS_BACKEND = LINTO_BACKEND
    settings.ENTITLEMENTS_CACHE_TIMEOUT = 300
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    # The locmem store outlives the test that filled it: start empty.
    django_cache.clear()
    return settings


# -- capabilities helper --


def test_normalize_coerces_the_known_keys_and_keeps_the_rest():
    raw = {"transcription": {"live": True}, "recording": "yes", "budget": 3}
    caps = normalize_capabilities(raw)
    assert caps["transcription"] == {"live": True, "async": False}
    assert caps["quickMeeting"] is True  # derived from live on an older Studio
    assert caps["recording"] is False
    assert caps["summary"] is False
    assert caps["budget"] == 3
    assert normalize_capabilities(None) == no_capabilities()


def test_nothing_decides_the_capabilities_without_linto(settings):
    settings.LINTO_FEATURE_ENABLED = False
    settings.LINTO_STUDIO_ENABLED = False
    settings.LINTO_ENTITLEMENTS_ENABLED = False
    assert user_linto_capabilities(factories.UserFactory()) is None


def test_kill_switch_grants_everything_without_a_backend_call(linto_settings):
    linto_settings.LINTO_ENTITLEMENTS_ENABLED = False
    with mock.patch("core.entitlements.get_user_entitlements") as backend_call:
        caps = user_linto_capabilities(factories.UserFactory())
    assert caps == all_capabilities()
    backend_call.assert_not_called()
    assert has_linto_capability(factories.UserFactory(), "transcription.async")
    assert has_linto_capability(factories.UserFactory(), "recording")


def test_local_backend_knows_nothing_about_linto(linto_settings):
    """upstream's `local` backend: no AI button at all (an instance without LinTO)."""
    linto_settings.ENTITLEMENTS_BACKEND = (
        "core.entitlements.backends.local.LocalEntitlementsBackend"
    )
    assert user_linto_capabilities(factories.UserFactory()) is None
    assert not has_linto_capability(factories.UserFactory(), "transcription.live")


def test_anonymous_holds_nothing(linto_settings):
    from django.contrib.auth.models import AnonymousUser  # noqa: PLC0415

    assert user_linto_capabilities(AnonymousUser()) == no_capabilities()
    assert user_linto_capabilities(None) == no_capabilities()


# -- LintoEntitlementsBackend --


@responses.activate
def test_backend_reads_the_capabilities_from_resolve(linto_settings):
    user = factories.UserFactory(sub="lemon-42", email="Alice@Linagora.com")
    responses.add(
        responses.POST,
        RESOLVE,
        json={"organizationId": "org-domain", "capabilities": LIVE_ONLY},
    )
    result = LintoEntitlementsBackend().get_user_entitlements(user.sub, user.email)
    assert result["can_create"] is True
    assert result["linto"]["transcription"] == {"live": True, "async": False}
    assert result["linto"]["quickMeeting"] is True
    assert result["linto"]["recording"] is False
    call = responses.calls[0].request
    assert call.headers["Authorization"] == "Bearer integration-key"
    assert "userScope=backoffice" in call.url
    assert json.loads(call.body) == {
        "provider": "meet:test",
        "subject": "lemon-42",
        "email": "alice@linagora.com",
    }


@responses.activate
def test_backend_answers_nothing_on_no_entitlement(linto_settings):
    user = factories.UserFactory(sub="nobody")
    responses.add(responses.POST, RESOLVE, status=404, json={"code": "no_entitlement"})
    result = LintoEntitlementsBackend().get_user_entitlements(user.sub, user.email)
    assert result == {"can_create": True, "linto": no_capabilities()}


@responses.activate
def test_backend_never_blocks_room_creation_when_studio_is_down(linto_settings):
    user = factories.UserFactory(sub="x")
    responses.add(responses.POST, RESOLVE, status=500, body="boom")
    result = LintoEntitlementsBackend().get_user_entitlements(user.sub, user.email)
    assert result == {"can_create": True, "linto": no_capabilities()}
    # A failure after a good answer keeps the cached one.
    responses.replace(
        responses.POST, RESOLVE, json={"organizationId": "o", "capabilities": LIVE_ONLY}
    )
    backend = LintoEntitlementsBackend()
    assert backend.get_user_entitlements(user.sub, user.email, force_refresh=True)[
        "linto"
    ]["transcription"]["live"]
    responses.replace(responses.POST, RESOLVE, status=503, body="down")
    stale = backend.get_user_entitlements(user.sub, user.email, force_refresh=True)
    assert stale["linto"]["transcription"]["live"] is True


@responses.activate
def test_backend_caches_per_user_and_honours_force_refresh(linto_settings):
    user = factories.UserFactory(sub="s1")
    other = factories.UserFactory(sub="s2")
    responses.add(
        responses.POST, RESOLVE, json={"organizationId": "o", "capabilities": LIVE_ONLY}
    )
    backend = LintoEntitlementsBackend()
    backend.get_user_entitlements(user.sub, user.email)
    backend.get_user_entitlements(user.sub, user.email)
    assert len(responses.calls) == 1
    backend.get_user_entitlements(other.sub, other.email)
    assert len(responses.calls) == 2
    backend.get_user_entitlements(user.sub, user.email, force_refresh=True)
    assert len(responses.calls) == 3


@responses.activate
def test_backend_kill_switch_never_calls_studio(linto_settings):
    linto_settings.LINTO_ENTITLEMENTS_ENABLED = False
    user = factories.UserFactory(sub="anyone")
    result = LintoEntitlementsBackend().get_user_entitlements(user.sub, user.email)
    assert result == {"can_create": True, "linto": all_capabilities()}
    assert not responses.calls


# -- users/me --


@responses.activate
def test_users_me_carries_the_linto_capabilities(linto_settings):
    user = factories.UserFactory(sub="me")
    responses.add(
        responses.POST, RESOLVE, json={"organizationId": "o", "capabilities": LIVE_ONLY}
    )
    client = APIClient()
    client.force_login(user)
    data = client.get("/api/v1.0/users/me/").json()
    assert data["can_create"] is True
    assert data["linto"]["transcription"] == {"live": True, "async": False}
    assert data["linto"]["recording"] is False


def test_users_me_with_the_kill_switch_off(linto_settings):
    linto_settings.LINTO_ENTITLEMENTS_ENABLED = False
    client = APIClient()
    client.force_login(factories.UserFactory())
    data = client.get("/api/v1.0/users/me/").json()
    assert data["linto"] == all_capabilities()


# -- server gate: deferred transcription needs transcription.async --


@pytest.fixture
def recording_setup(linto_settings):
    linto_settings.RECORDING_ENABLE = True
    linto_settings.METADATA_COLLECTOR_ENABLED = False
    room = factories.RoomFactory()
    user = factories.UserFactory(sub="owner")
    room.accesses.create(user=user, role="owner")
    client = APIClient()
    client.force_login(user)
    with (
        mock.patch("core.api.viewsets.get_worker_service"),
        mock.patch("core.api.viewsets.WorkerServiceMediator"),
    ):
        yield client, room


def _start(client, room, body):
    return client.post(
        f"/api/v1.0/rooms/{room.id}/start-recording/", body, format="json"
    )


@responses.activate
def test_transcribe_is_refused_without_the_async_capability(recording_setup):
    client, room = recording_setup
    responses.add(
        responses.POST, RESOLVE, json={"organizationId": "o", "capabilities": LIVE_ONLY}
    )
    res = _start(
        client, room, {"mode": "screen_recording", "options": {"transcribe": True}}
    )
    assert res.status_code == 403
    assert res.json()["code"] == "no_entitlement"
    assert res.json()["feature"] == "transcription.async"
    res = _start(client, room, {"mode": "transcript"})
    assert res.status_code == 403
    # A plain video recording is not a LinTO feature.
    res = _start(client, room, {"mode": "screen_recording"})
    assert res.status_code == 201


@responses.activate
def test_transcribe_is_allowed_with_the_async_capability(recording_setup):
    client, room = recording_setup
    responses.add(
        responses.POST,
        RESOLVE,
        json={
            "organizationId": "o",
            "capabilities": {"transcription": {"live": True, "async": True}},
        },
    )
    res = _start(
        client, room, {"mode": "screen_recording", "options": {"transcribe": True}}
    )
    assert res.status_code == 201


def test_transcribe_is_allowed_with_the_kill_switch_off(recording_setup, settings):
    settings.LINTO_ENTITLEMENTS_ENABLED = False
    client, room = recording_setup
    res = _start(client, room, {"mode": "transcript"})
    assert res.status_code == 201
