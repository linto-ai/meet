"""OIDC access token authentication on the core API, for native apps.

The web frontend authenticates with the Django session cookie it gets from the
server-side Authorization Code flow. A native app (e.g. the Twake Visio mobile
app) cannot hold that cookie without embedding a WebView, which RFC 8252
rules out. It runs its own Authorization Code + PKCE flow against the OIDC
provider, as a public client, and sends the access token as a Bearer header.
This backend introspects that token (RFC 7662) and authenticates its user, so
the core API serves the native app exactly as it serves the web frontend: the
same views, the same role-based permissions.
"""

import logging

from django.conf import settings
from django.core.exceptions import SuspiciousOperation

from lasuite.oidc_resource_server.authentication import ResourceServerAuthentication
from requests.exceptions import RequestException
from rest_framework import exceptions

logger = logging.getLogger(__name__)


class CoreAPIResourceServerAuthentication(ResourceServerAuthentication):
    """Authenticate core API requests with an access token of the OIDC provider.

    Inert unless OIDC_RS_CORE_API_ENABLED is True: it then returns None before
    reading the Authorization header, deferring every request to the next
    authentication backend.

    Only tokens issued to a client listed in OIDC_RS_CORE_API_ALLOWED_AUDIENCES
    are accepted, checked BEFORE any user is looked up or created. Without the
    allowlist, any active token of the provider carrying one of OIDC_RS_SCOPES
    would open the core API, whichever application it was issued to.

    A Bearer token that is not an active access token of an allowed client is
    deferred to the next backend instead of failing the request. Some core API
    endpoints also take a LiveKit room token as Bearer (start-recording,
    stop-recording list it after the default backends): failing here would
    refuse those tokens before LiveKitTokenAuthentication sees them. When no
    backend authenticates, DRF still answers 401.

    This also turns lasuite's SuspiciousOperation on an inactive or expired
    token into that 401, where the parent class lets Django answer 400 — a
    native app refreshes its token on 401, not on 400.
    """

    def __init__(self):
        """Build the lasuite backend only when the feature is enabled."""
        if not settings.OIDC_RS_CORE_API_ENABLED:
            self.backend = None
            return
        super().__init__()

    def authenticate(self, request):
        """Return (user, introspection response) for an allowed token, else None."""
        if not settings.OIDC_RS_CORE_API_ENABLED:
            return None

        # A malformed "Bearer" header raises AuthenticationFailed here: a 401.
        access_token = self.get_access_token(request)
        user_info = self._introspect(access_token) if access_token else None
        user = self._allowed_user(access_token, user_info) if user_info else None
        if user is None:
            return None

        # Same contract as the parent class: the views may read it.
        request.resource_server_token_audience = self.backend.token_origin_audience

        return user, user_info

    def _introspect(self, access_token):
        """The introspection response of an active token, or None."""
        try:
            return self.backend.get_user_info_with_introspection(access_token)
        except (SuspiciousOperation, exceptions.AuthenticationFailed) as exc:
            # Inactive, expired or unknown token, missing scope, or a token of
            # another kind (e.g. a LiveKit room token).
            logger.debug("Bearer token not accepted by introspection: %s", exc)
        except RequestException:
            # The provider is unreachable: nothing can be authenticated here,
            # and the request may still carry a token another backend accepts.
            logger.exception("Token introspection failed")
        return None

    def _allowed_user(self, access_token, user_info):
        """The active user of a token issued to an allowed client, or None.

        The backend refuses a client outside the allowlist before any user
        lookup, so that a token of another application never creates a user."""
        try:
            user = self.backend.get_or_create_user(access_token, None, user_info)
        except SuspiciousOperation as exc:
            logger.info("Core API resource server login failed: %s", exc)
            return None

        return user if user is not None and user.is_active else None
