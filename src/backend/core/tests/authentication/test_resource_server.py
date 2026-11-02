"""
Tests for CoreAPIResourceServerAuthentication: OIDC access tokens on the core API.

A native app sends the access token it got from the OIDC provider (Authorization
Code + PKCE, public client) as a Bearer header; Meet introspects it.
"""

# pylint: disable=redefined-outer-name,unused-argument

from unittest import mock
from urllib.parse import parse_qs

import pytest
import responses
from requests.exceptions import ConnectionError as RequestsConnectionError
from rest_framework.test import APIClient

from core import utils
from core.factories import RoomFactory, UserFactory
from core.models import Recording, Room, User

pytestmark = pytest.mark.django_db

OP_URL = "https://oidc.example.com"
INTROSPECTION_URL = f"{OP_URL}/introspect"
MOBILE_CLIENT_ID = "twake-visio"
TOKEN = "opaque-access-token-of-the-mobile-app"


@pytest.fixture
def resource_server(settings):
    """Meet as a resource server on its core API, for the mobile client only."""
    settings.OIDC_RS_CORE_API_ENABLED = True
    settings.OIDC_RS_CORE_API_ALLOWED_AUDIENCES = [MOBILE_CLIENT_ID]
    settings.OIDC_RS_CLIENT_ID = "meet"
    settings.OIDC_RS_CLIENT_SECRET = "meet-secret"
    settings.OIDC_RS_SCOPES = ["openid"]
    settings.OIDC_OP_URL = OP_URL
    settings.OIDC_OP_INTROSPECTION_ENDPOINT = INTROSPECTION_URL
    return settings


def introspection(
    sub,
    client_id=MOBILE_CLIENT_ID,
    active=True,
    scope="openid email profile offline_access",
):
    """An RFC 7662 introspection response, as the OIDC provider returns it."""
    return {
        "iss": OP_URL,
        "active": active,
        "sub": sub,
        "client_id": client_id,
        "scope": scope,
    }


def bearer(token=TOKEN):
    """An API client that authenticates the way the mobile app does."""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


# ==============================
# Disabled (the default)
# ==============================


@responses.activate
def test_disabled_by_default_never_introspects(settings):
    """Without OIDC_RS_CORE_API_ENABLED, a Bearer token is ignored, unread."""
    assert settings.OIDC_RS_CORE_API_ENABLED is False
    settings.OIDC_OP_URL = OP_URL
    settings.OIDC_OP_INTROSPECTION_ENDPOINT = INTROSPECTION_URL
    user = UserFactory()
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection(user.sub))

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 401
    assert len(responses.calls) == 0


# ==============================
# Accepted tokens
# ==============================


@responses.activate
def test_allowed_token_authenticates_its_user(resource_server):
    """An active token of the mobile client authenticates the user of its sub,
    and Meet introspects it with its own resource server credentials."""
    user = UserFactory()
    UserFactory.create_batch(2)
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection(user.sub))

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)
    assert len(responses.calls) == 1
    sent = parse_qs(responses.calls[0].request.body)
    assert sent == {
        "client_id": ["meet"],
        "client_secret": ["meet-secret"],
        "token": [TOKEN],
    }


@responses.activate
def test_allowed_token_creates_an_unknown_user(resource_server):
    """A first sign-in from the app creates the user, as the external API does."""
    resource_server.OIDC_CREATE_USER = True
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("new-sub"))

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    assert response.json()["id"] == str(User.objects.get(sub="new-sub").id)


@responses.activate
def test_allowed_token_does_not_create_a_user_when_creation_is_off(resource_server):
    """OIDC_CREATE_USER=False is honoured: an unknown sub is not authenticated."""
    resource_server.OIDC_CREATE_USER = False
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("new-sub"))

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 401
    assert not User.objects.filter(sub="new-sub").exists()


@responses.activate
def test_allowed_token_writes_without_csrf(resource_server):
    """A Bearer request carries no cookie, so no CSRF token: writes must pass."""
    user = UserFactory()
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection(user.sub))
    client = APIClient(enforce_csrf_checks=True)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    response = client.post("/api/v1.0/rooms/", {"name": "from the app"})

    assert response.status_code == 201
    room = Room.objects.get()
    assert room.accesses.filter(role="owner", user=user).exists()


# ==============================
# Refused tokens: always 401
# ==============================


@responses.activate
def test_token_of_another_client_is_refused_before_any_user_creation(resource_server):
    """A valid token issued to another application of the same provider must
    not open the core API, and must not leave a user behind."""
    resource_server.OIDC_CREATE_USER = True
    user = UserFactory()
    responses.add(
        responses.POST,
        INTROSPECTION_URL,
        json=introspection(user.sub, client_id="some-other-app"),
    )

    assert bearer().get("/api/v1.0/users/me/").status_code == 401

    responses.replace(
        responses.POST,
        INTROSPECTION_URL,
        json=introspection("unknown-sub", client_id="some-other-app"),
    )

    assert bearer().get("/api/v1.0/users/me/").status_code == 401
    assert not User.objects.filter(sub="unknown-sub").exists()


@responses.activate
def test_empty_allowlist_accepts_no_token(resource_server):
    """Enabled without an allowlist, nothing gets in."""
    resource_server.OIDC_RS_CORE_API_ALLOWED_AUDIENCES = []
    user = UserFactory()
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection(user.sub))

    assert bearer().get("/api/v1.0/users/me/").status_code == 401


@responses.activate
def test_inactive_token_answers_401_not_400(resource_server):
    """An expired token must answer 401, the status on which the app refreshes.
    The parent lasuite class lets Django answer 400 here."""
    responses.add(
        responses.POST, INTROSPECTION_URL, json={"iss": OP_URL, "active": False}
    )

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 401


@responses.activate
def test_token_without_required_scope_is_refused(resource_server):
    """OIDC_RS_SCOPES still applies."""
    user = UserFactory()
    responses.add(
        responses.POST,
        INTROSPECTION_URL,
        json=introspection(user.sub, scope="email profile"),
    )

    assert bearer().get("/api/v1.0/users/me/").status_code == 401


@responses.activate
def test_token_of_an_inactive_user_is_refused(resource_server):
    """A disabled account stays out, whatever its token."""
    user = UserFactory(is_active=False)
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection(user.sub))

    assert bearer().get("/api/v1.0/users/me/").status_code == 401


@pytest.mark.parametrize("status", [400, 401, 500, 503])
@responses.activate
def test_introspection_error_answers_401(resource_server, status):
    """The provider refusing or failing the introspection is a 401, not a 500."""
    responses.add(responses.POST, INTROSPECTION_URL, status=status)

    assert bearer().get("/api/v1.0/users/me/").status_code == 401


@responses.activate
def test_unreachable_provider_answers_401(resource_server):
    """A network error towards the provider is a 401, not a 500."""
    responses.add(
        responses.POST, INTROSPECTION_URL, body=RequestsConnectionError("down")
    )

    assert bearer().get("/api/v1.0/users/me/").status_code == 401


@pytest.mark.parametrize("header", ["Bearer", "Bearer two parts"])
@responses.activate
def test_malformed_bearer_header_answers_401(resource_server, header):
    """A malformed header is refused by the API, never introspected."""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=header)

    assert client.get("/api/v1.0/users/me/").status_code == 401
    assert len(responses.calls) == 0


# ==============================
# The other backends are untouched
# ==============================


@responses.activate
def test_session_still_authenticates_without_introspection(resource_server):
    """The web frontend keeps its session cookie; nothing is introspected."""
    user = UserFactory()
    client = APIClient()
    client.force_login(user)

    response = client.get("/api/v1.0/users/me/")

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)
    assert len(responses.calls) == 0


@responses.activate
def test_livekit_token_still_reaches_its_backend(resource_server):
    """start-recording lists the default backends BEFORE LiveKitTokenAuthentication.
    A LiveKit room token is not an access token of the provider: it must be
    deferred to LiveKitTokenAuthentication, not refused on the way."""
    resource_server.RECORDING_ENABLE = True
    responses.add(
        responses.POST, INTROSPECTION_URL, json={"iss": OP_URL, "active": False}
    )
    user = UserFactory()
    room = RoomFactory(configuration={"screen_recording_permission": "authenticated"})
    livekit_token = utils.generate_token(
        room=str(room.id), user=user, identity=f"@{user.sub}:localhost:DEVICE"
    )

    with (
        mock.patch("core.api.viewsets.get_worker_service"),
        mock.patch("core.api.viewsets.WorkerServiceMediator") as mediator,
        mock.patch("core.api.permissions.ParticipantsManagement") as presence,
    ):
        presence.return_value.check_if_in_meeting.return_value = True
        response = bearer(livekit_token).post(
            f"/api/v1.0/rooms/{room.id}/start-recording/",
            {"mode": "screen_recording"},
            format="json",
        )

    assert response.status_code == 201
    assert Recording.objects.filter(room=room).count() == 1
    mediator.return_value.start.assert_called_once()
    # The token was offered to the provider first, and refused there.
    assert len(responses.calls) == 1
