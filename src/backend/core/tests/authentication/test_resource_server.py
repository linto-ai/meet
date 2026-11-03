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
from core.authentication.backends import OIDCAuthenticationBackend
from core.factories import RoomFactory, UserFactory
from core.models import Recording, Room, User
from core.services.provisional_user_service import ProvisionalUserService

pytestmark = pytest.mark.django_db

OP_URL = "https://oidc.example.com"
INTROSPECTION_URL = f"{OP_URL}/introspect"
USERINFO_URL = f"{OP_URL}/userinfo"
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
    settings.OIDC_OP_USER_ENDPOINT = USERINFO_URL
    settings.OIDC_OP_JWKS_ENDPOINT = f"{OP_URL}/jwks"
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


def userinfo(sub):
    """The userinfo response of a token with the scopes of the app."""
    return {
        "sub": sub,
        "email": "alice@example.com",
        "given_name": "Alice",
        "family_name": "Liddell",
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
    """A first sign-in from the app creates the user with the email and names
    of userinfo, asked once with the token of the app."""
    resource_server.OIDC_CREATE_USER = True
    resource_server.OIDC_USERINFO_FULLNAME_FIELDS = ["given_name", "family_name"]
    resource_server.OIDC_USERINFO_SHORTNAME_FIELD = "given_name"
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("new-sub"))
    responses.add(responses.GET, USERINFO_URL, json=userinfo("new-sub"))

    response = bearer().get("/api/v1.0/users/me/")
    bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    user = User.objects.get(sub="new-sub")
    assert response.json()["id"] == str(user.id)
    assert response.json()["email"] == "alice@example.com"
    assert (user.full_name, user.short_name) == ("Alice Liddell", "Alice")
    userinfo_calls = [c for c in responses.calls if c.request.url == USERINFO_URL]
    assert len(userinfo_calls) == 1
    assert userinfo_calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


@responses.activate
def test_allowed_token_does_not_create_a_user_when_creation_is_off(resource_server):
    """OIDC_CREATE_USER=False is honoured: an unknown sub is not authenticated."""
    resource_server.OIDC_CREATE_USER = False
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("new-sub"))
    responses.add(responses.GET, USERINFO_URL, json=userinfo("new-sub"))

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 401
    assert not User.objects.filter(sub="new-sub").exists()


# ==============================
# One account per person, whatever signs in first
# ==============================


def provisional_user(email="alice@example.com"):
    """A user an application provisioned by email (Twake Chat, the calendar)."""
    user = User(sub=None, email=email)
    user.set_unusable_password()
    user.save()
    return user


@pytest.fixture
def email_fallback(resource_server):
    """The settings the provisioning of users by email requires."""
    resource_server.OIDC_FALLBACK_TO_EMAIL_FOR_IDENTIFICATION = True
    resource_server.OIDC_USER_SUB_FIELD_IMMUTABLE = False
    return resource_server


@responses.activate
def test_app_after_an_application_finds_the_provisional_user(email_fallback):
    """The app signs in to the account an application provisioned by email,
    with its rooms, and gives it its sub."""
    user = provisional_user()
    RoomFactory(users=[(user, "owner")])
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(responses.GET, USERINFO_URL, json=userinfo("alice"))

    me = bearer().get("/api/v1.0/users/me/")
    rooms = bearer().get("/api/v1.0/rooms/")

    assert me.json()["id"] == str(user.id)
    assert rooms.json()["count"] == 1
    user.refresh_from_db()
    assert user.sub == "alice"
    assert User.objects.count() == 1


@responses.activate
def test_web_after_the_app_finds_the_same_user(email_fallback):
    """A web sign-in after the app finds the user of the app by its sub."""
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(responses.GET, USERINFO_URL, json=userinfo("alice"))
    bearer().get("/api/v1.0/users/me/")

    with mock.patch.object(
        OIDCAuthenticationBackend, "get_userinfo", return_value=userinfo("alice")
    ):
        web_user = OIDCAuthenticationBackend().get_or_create_user("a", "i", {})

    assert web_user == User.objects.get()


@responses.activate
def test_application_after_the_app_finds_the_same_user(email_fallback):
    """An application acting for the email of a user of the app finds that
    user, instead of provisioning a second one."""
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(responses.GET, USERINFO_URL, json=userinfo("alice"))
    bearer().get("/api/v1.0/users/me/")

    user, created = ProvisionalUserService().get_or_create(
        "alice@example.com", client_id="twake-chat"
    )

    assert not created
    assert user == User.objects.get(sub="alice")


@responses.activate
def test_sub_of_the_introspection_wins_over_userinfo(resource_server):
    """The provider computes the sub for the client that asks: the introspection
    one is Meet's, the userinfo one the app's."""
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(responses.GET, USERINFO_URL, json=userinfo("alice@example.com"))

    assert bearer().get("/api/v1.0/users/me/").status_code == 200
    assert User.objects.get().sub == "alice"


@responses.activate
def test_userinfo_without_email_creates_the_user_from_its_sub(email_fallback):
    """Without an email, nothing else can be matched: the user of the sub."""
    provisional_user()
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(responses.GET, USERINFO_URL, json={"sub": "alice"})

    assert bearer().get("/api/v1.0/users/me/").status_code == 200
    assert User.objects.get(sub="alice").email is None


@responses.activate
def test_two_users_with_the_email_answer_401(email_fallback):
    """Users sharing the email: the web refuses the sign-in, so does the app."""
    provisional_user()
    UserFactory(sub="someone-else", email="alice@example.com")
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(responses.GET, USERINFO_URL, json=userinfo("alice"))

    assert bearer().get("/api/v1.0/users/me/").status_code == 401
    assert not User.objects.filter(sub="alice").exists()


@pytest.mark.parametrize(
    "failure", [{"status": 500}, {"body": RequestsConnectionError("down")}]
)
@responses.activate
def test_userinfo_failure_answers_401_and_creates_nobody(resource_server, failure):
    """Without userinfo, no user is created from the sub alone."""
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(responses.GET, USERINFO_URL, **failure)

    assert bearer().get("/api/v1.0/users/me/").status_code == 401
    assert not User.objects.exists()


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
