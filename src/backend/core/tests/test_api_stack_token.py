"""
Test the stack-token endpoint: server-side exchange of the OIDC id token for a
Twake stack (cozy-stack) access token.
"""

# pylint: disable=W0621

import json

from django.conf import settings
from django.utils.crypto import get_random_string

import pytest
import requests
import responses
from rest_framework.test import APIClient

from core.factories import UserFactory
from core.models import User

pytestmark = pytest.mark.django_db

URL = "/api/v1.0/stack-token/"
INSTANCE = "bob.twake.app"
EXCHANGE_URL = f"https://{INSTANCE}/auth/token_exchange"
SECRETS = ("refresh-secret", "client-id-secret", "client-secret-secret")


def login(instance=INSTANCE, id_token="the-id-token", **client_options):
    """A client logged in with a session, holding or not an OIDC id token."""
    user = UserFactory()
    client = APIClient(**client_options)
    client.force_login(user)
    # update() skips the model validation: stored values are untrusted anyway.
    # It comes after the login, whose save() validates the model.
    User.objects.filter(pk=user.pk).update(twake_instance=instance)
    if id_token:
        session = client.session
        session["oidc_id_token"] = id_token
        session.save()
    return client


def stack_body(**extra):
    """What the stack answers, with fields that must never leak."""
    return {
        "access_token": "stack-access",
        "refresh_token": SECRETS[0],
        "client_id": SECRETS[1],
        "client_secret": SECRETS[2],
        **extra,
    }


def assert_no_leak(response):
    """No secret of the stack in the response."""
    text = response.content.decode()
    for secret in SECRETS:
        assert secret not in text


def test_stack_token_anonymous():
    """An anonymous request is refused."""
    response = APIClient().post(URL)
    assert response.status_code in (401, 403)


@responses.activate
def test_stack_token_returns_access_token_only():
    """The id token is exchanged; only access_token and platform_url come back."""
    responses.post(EXCHANGE_URL, json=stack_body(), status=200)

    response = login().post(URL)

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "stack-access",
        "platform_url": f"https://{INSTANCE}",
    }
    assert_no_leak(response)
    call = responses.calls[0].request
    assert json.loads(call.body) == {
        "id_token": "the-id-token",
        "exchange_type": "app",
    }
    assert call.headers["Accept"] == "application/json"


@responses.activate
def test_stack_token_without_instance():
    """A user with no Twake instance gets a clear conflict, nothing is fetched."""
    response = login(instance=None).post(URL)

    assert response.status_code in (400, 409)
    assert response.json().get("error") or response.json().get("code")
    assert len(responses.calls) == 0


@responses.activate
def test_stack_token_without_id_token_in_session():
    """No id token in the session: unauthorized, nothing is fetched."""
    response = login(id_token=None).post(URL)

    assert response.status_code == 401
    assert len(responses.calls) == 0


@responses.activate
@pytest.mark.parametrize("status", [400, 401, 403])
def test_stack_token_id_token_rejected(status):
    """The stack rejects the id token: 401, the browser must sign in again."""
    responses.post(
        EXCHANGE_URL,
        json={**stack_body(), "detail": "stack-internal-detail"},
        status=status,
    )

    response = login().post(URL)

    assert response.status_code == 401
    assert response.json() == {"error": "id_token_rejected"}
    assert "stack-internal-detail" not in response.content.decode()
    assert "stack-access" not in response.content.decode()
    assert_no_leak(response)


@responses.activate
@pytest.mark.parametrize("status", [404, 500, 502, 503])
def test_stack_token_stack_error(status):
    """Any other non-2xx answer of the stack is a 502, its body is not echoed."""
    responses.post(
        EXCHANGE_URL,
        json={**stack_body(), "detail": "stack-internal-detail"},
        status=status,
    )

    response = login().post(URL)

    assert response.status_code == 502
    assert response.json() == {"error": "stack_exchange_failed"}
    assert "stack-internal-detail" not in response.content.decode()
    assert "stack-access" not in response.content.decode()
    assert_no_leak(response)


@responses.activate
@pytest.mark.parametrize(
    "body",
    ["not json", json.dumps({"refresh_token": "x"}), json.dumps({"access_token": ""})],
)
def test_stack_token_unusable_answer(body):
    """A 200 that is not JSON or has no access_token is a 502, not a sign-in."""
    responses.post(EXCHANGE_URL, body=body, status=200)

    response = login().post(URL)

    assert response.status_code == 502
    assert response.json() == {"error": "stack_exchange_failed"}


@responses.activate
def test_stack_token_stack_timeout():
    """A timeout of the stack is a 502."""
    responses.post(EXCHANGE_URL, body=requests.exceptions.Timeout("slow"))

    response = login().post(URL)

    assert response.status_code == 502
    assert response.json() == {"error": "stack_exchange_failed"}


@responses.activate
@pytest.mark.parametrize(
    "instance",
    [
        "https://bob.twake.app",
        "bob.twake.app/path",
        "bob.twake.app:8443",
        "user:pass@bob.twake.app",
        "evil.com#@bob.twake.app",
        "127.0.0.1",
        "localhost",
        "10.0.0.5",
        "169.254.169.254",
        "[::1]",
    ],
)
def test_stack_token_untrusted_instance_is_never_fetched(instance):
    """An instance that is not a bare public hostname is never requested."""
    # Block any outbound call: an unregistered URL raises ConnectionError.
    response = login(instance=instance).post(URL)

    assert response.status_code in (400, 409)
    assert len(responses.calls) == 0


@responses.activate
def test_stack_token_session_without_csrf_token():
    """A session-authenticated POST without a CSRF token is refused, nothing is exchanged."""
    responses.post(EXCHANGE_URL, json=stack_body(), status=200)

    response = login(enforce_csrf_checks=True).post(URL)

    assert response.status_code == 403
    assert response.json()["detail"].startswith("CSRF Failed")
    assert len(responses.calls) == 0


@responses.activate
def test_stack_token_session_with_csrf_token():
    """With a valid CSRF token the normal flow still answers 200."""
    responses.post(EXCHANGE_URL, json=stack_body(), status=200)
    client = login(enforce_csrf_checks=True)
    csrf_secret = get_random_string(32)
    client.cookies[settings.CSRF_COOKIE_NAME] = csrf_secret

    response = client.post(URL, HTTP_X_CSRFTOKEN=csrf_secret)

    assert response.status_code == 200
    assert response.json()["platform_url"] == f"https://{INSTANCE}"
