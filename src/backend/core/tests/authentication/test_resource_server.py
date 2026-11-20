"""
Tests for CoreAPIResourceServerAuthentication: OIDC access tokens on the core API.

A native app sends the access token it got from the OIDC provider (Authorization
Code + PKCE, public client) as a Bearer header; Meet introspects it.
"""

# pylint: disable=redefined-outer-name,unused-argument

from unittest import mock
from urllib.parse import parse_qs

from django.core.cache import cache
from django.db.models import QuerySet

import pytest
import responses
from requests.exceptions import ConnectionError as RequestsConnectionError
from rest_framework.test import APIClient

from core import utils
from core.external_api.authentication import (
    _link_provisional_user,
    userinfo_cache_key,
)
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
    introspections = [c for c in responses.calls if c.request.url == INTROSPECTION_URL]
    assert len(introspections) == 1
    sent = parse_qs(introspections[0].request.body)
    assert sent == {
        "client_id": ["meet"],
        "client_secret": ["meet-secret"],
        "token": [TOKEN],
    }


USERINFO_URL = f"{OP_URL}/userinfo"


@pytest.fixture(autouse=True)
def no_refreshed_token():
    """Each test starts with no access token whose userinfo was read. Only these
    keys: the cache is shared with the tests other workers run meanwhile."""
    cache.delete_many(
        [userinfo_cache_key(token) for token in (TOKEN, "next-access-token")]
    )


@pytest.fixture
def with_userinfo(resource_server):
    """The provider's userinfo endpoint, as the web sign-in uses it."""
    resource_server.OIDC_OP_USER_ENDPOINT = USERINFO_URL
    resource_server.OIDC_CREATE_USER = True
    return resource_server


def userinfo(sub="alice", **claims):
    """A userinfo response for the app's token."""
    claims = {
        "sub": sub,
        "email": "alice@example.com",
        "email_verified": True,
        "given_name": "Alice",
        "usual_name": "Martin",
        "workplaceFqdn": "alice.twake.example.com",
        **claims,
    }
    responses.add(
        responses.GET,
        USERINFO_URL,
        json={key: value for key, value in claims.items() if value is not None},
    )


def userinfo_calls():
    """The calls Meet made to the userinfo endpoint."""
    return [c for c in responses.calls if c.request.url == USERINFO_URL]


@responses.activate
def test_allowed_token_creates_a_complete_user(with_userinfo):
    """A first sign-in from the app creates the user with what the web sign-in
    stores: email, names and Twake instance from userinfo, asked with the token."""
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    userinfo()

    with mock.patch("core.authentication.backends.get_user_entitlements") as warm:
        response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    user = User.objects.get(sub="alice")
    assert response.json()["id"] == str(user.id)
    assert user.email == "alice@example.com"
    assert user.full_name == "Alice Martin"
    assert user.short_name == "Alice"
    assert user.twake_instance == "alice.twake.example.com"
    warm.assert_called_once()
    assert userinfo_calls()[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


@responses.activate
def test_userinfo_without_twake_instance_creates_the_user(with_userinfo):
    """A provider that exports no workplaceFqdn leaves the instance empty."""
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    userinfo(workplaceFqdn=None)

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    user = User.objects.get(sub="alice")
    assert user.full_name == "Alice Martin"
    assert user.twake_instance is None


@responses.activate
def test_allowed_token_does_not_create_a_user_when_creation_is_off(with_userinfo):
    """OIDC_CREATE_USER=False is honoured: an unknown sub is not authenticated."""
    with_userinfo.OIDC_CREATE_USER = False
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    userinfo()

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 401
    assert not User.objects.filter(sub="alice").exists()


@responses.activate
def test_userinfo_failure_answers_401_and_creates_nobody(with_userinfo):
    """Without userinfo, no incomplete user is created from the sub alone."""
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(
        responses.GET, USERINFO_URL, body=RequestsConnectionError("unreachable")
    )

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 401
    assert not User.objects.filter(sub="alice").exists()


@responses.activate
def test_known_user_is_refreshed_once_per_access_token(with_userinfo):
    """As the web sign-in refreshes the user at each sign-in, a known user is
    refreshed at the first request of each access token, not at every one."""
    user = UserFactory(sub="alice", full_name="Old Name")
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    userinfo()

    for token in (TOKEN, TOKEN, "next-access-token"):
        assert bearer(token).get("/api/v1.0/users/me/").status_code == 200

    user.refresh_from_db()
    assert user.full_name == "Alice Martin"
    assert len(userinfo_calls()) == 2


@responses.activate
def test_known_user_is_served_when_userinfo_fails(with_userinfo):
    """A failed refresh does not lock a known user out."""
    user = UserFactory(sub="alice")
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    responses.add(
        responses.GET, USERINFO_URL, body=RequestsConnectionError("unreachable")
    )

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)


# ==============================
# Users provisioned by email
# ==============================


@pytest.fixture
def link_by_email(with_userinfo):
    """Unknown subs are matched by the verified email of their userinfo."""
    with_userinfo.OIDC_RS_LINK_USERS_BY_EMAIL = True
    return with_userinfo


def provisional_user(email="alice@example.com"):
    """A user an application provisioned by email, before any sign-in."""
    return UserFactory(sub=None, email=email)


@responses.activate
def test_unknown_sub_takes_over_the_provisional_user(link_by_email):
    """The app signs in to the account an application provisioned, which keeps
    the sub and is completed from userinfo: the next request needs no userinfo."""
    user = provisional_user()
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    userinfo(email="Alice@example.com")

    for _ in range(2):
        response = bearer().get("/api/v1.0/users/me/")
        assert response.status_code == 200
        assert response.json()["id"] == str(user.id)

    user.refresh_from_db()
    assert user.sub == "alice"
    assert user.full_name == "Alice Martin"
    assert user.twake_instance == "alice.twake.example.com"
    assert User.objects.count() == 1
    assert len(userinfo_calls()) == 1


@responses.activate
def test_provisional_user_is_linked_even_when_creation_is_off(link_by_email):
    """Linking creates no account: OIDC_CREATE_USER=False does not prevent it."""
    link_by_email.OIDC_CREATE_USER = False
    user = provisional_user()
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    userinfo()

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)


@pytest.mark.parametrize("email_verified", [False, "false", None])
@responses.activate
def test_unverified_email_never_takes_over_an_account(link_by_email, email_verified):
    """An email the provider does not say is verified (false or absent) could be
    anyone's: the provisional user stays untouched, the sub gets its own."""
    provisional = provisional_user()
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("attacker"))
    userinfo(sub="attacker", email_verified=email_verified)

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    assert response.json()["id"] != str(provisional.id)
    provisional.refresh_from_db()
    assert provisional.sub is None


def test_a_provisional_user_is_taken_by_one_sub_only(link_by_email):
    """Two first tokens of different subs read the provisional user at once:
    only the first write lands, the second takes nothing."""
    provisional = provisional_user()
    stale = User.objects.get(pk=provisional.pk)
    assert _link_provisional_user("sub-a", "alice@example.com", MOBILE_CLIENT_ID)

    with mock.patch.object(QuerySet, "first", return_value=stale):
        assert (
            _link_provisional_user("sub-b", "alice@example.com", MOBILE_CLIENT_ID)
            is None
        )

    provisional.refresh_from_db()
    assert provisional.sub == "sub-a"


@responses.activate
def test_email_of_a_user_with_another_sub_is_refused(link_by_email):
    """Only a user without sub is taken over; a signed-in user of the email is
    refused, as the external API does with the email fallback."""
    link_by_email.OIDC_FALLBACK_TO_EMAIL_FOR_IDENTIFICATION = True
    UserFactory(sub="alice-on-the-web", email="alice@example.com")
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    userinfo()

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 401
    assert not User.objects.filter(sub="alice").exists()


@responses.activate
def test_provisional_user_is_ignored_when_linking_is_off(with_userinfo):
    """Off by default: the sub gets an account of its own."""
    provisional = provisional_user()
    responses.add(responses.POST, INTROSPECTION_URL, json=introspection("alice"))
    userinfo()

    response = bearer().get("/api/v1.0/users/me/")

    assert response.status_code == 200
    assert response.json()["id"] != str(provisional.id)
    provisional.refresh_from_db()
    assert provisional.sub is None


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
def test_external_api_refuses_another_client_before_any_user_creation(
    resource_server,
):
    """The external API takes the same resource server settings: a token of
    another application gets nothing there either, whatever its scopes."""
    resource_server.OIDC_CREATE_USER = True
    user = UserFactory()
    RoomFactory(users=[(user, "owner")])
    scope = "openid rooms:list rooms:create"
    responses.add(
        responses.POST,
        INTROSPECTION_URL,
        json=introspection(user.sub, client_id="some-other-app", scope=scope),
    )

    assert bearer().get("/external-api/v1.0/rooms/").status_code == 401
    assert bearer().post("/external-api/v1.0/rooms/", {}).status_code == 401
    assert Room.objects.count() == 1

    responses.replace(
        responses.POST,
        INTROSPECTION_URL,
        json=introspection("unknown-sub", client_id="some-other-app", scope=scope),
    )

    assert bearer().get("/external-api/v1.0/rooms/").status_code == 401
    assert not User.objects.filter(sub="unknown-sub").exists()


@responses.activate
def test_external_api_takes_an_allowed_client(resource_server):
    """The listed clients keep the external API, within their scopes."""
    user = UserFactory()
    RoomFactory(users=[(user, "owner")])
    responses.add(
        responses.POST,
        INTROSPECTION_URL,
        json=introspection(user.sub, scope="openid rooms:list"),
    )

    response = bearer().get("/external-api/v1.0/rooms/")

    assert response.status_code == 200
    assert response.json()["count"] == 1


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
    """start-recording lists the default backends BEFORE LiveKitTokenAuthentication:
    a LiveKit room token still reaches it, and is never introspected."""
    resource_server.RECORDING_ENABLE = True
    user = UserFactory()
    room = RoomFactory(configuration={"screen_recording_permission": "authenticated"})
    livekit_token = utils.generate_token(room=str(room.id), user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"X-LiveKit-Token {livekit_token}")

    with (
        mock.patch("core.api.viewsets.get_worker_service"),
        mock.patch("core.api.viewsets.WorkerServiceMediator") as mediator,
        mock.patch("core.api.permissions.ParticipantsManagement") as presence,
    ):
        presence.return_value.check_if_in_meeting.return_value = True
        response = client.post(
            f"/api/v1.0/rooms/{room.id}/start-recording/",
            {"mode": "screen_recording"},
            format="json",
        )

    assert response.status_code == 201
    assert Recording.objects.filter(room=room).count() == 1
    mediator.return_value.start.assert_called_once()
    assert len(responses.calls) == 0
