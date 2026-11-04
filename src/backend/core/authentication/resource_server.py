"""OIDC access token authentication on the core API, for native apps.

A native app cannot hold the session cookie without a WebView, which RFC 8252
rules out. It runs its own Authorization Code + PKCE flow and sends the access
token as a Bearer header, which this backend introspects (RFC 7662).
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

    Only tokens issued to a client in OIDC_RS_CORE_API_ALLOWED_AUDIENCES are
    accepted: otherwise any token of the provider with one of OIDC_RS_SCOPES
    would open the core API, whichever application it was issued to.

    A refused Bearer token is deferred to the next backend, not failed:
    start-recording and stop-recording also take a LiveKit room token as
    Bearer. With no backend left, DRF answers 401, where lasuite would let
    Django answer 400; a native app refreshes its token on 401 only.
    """

    def __init__(self):
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
        """The active user of a token issued to an allowed client, or None."""
        try:
            user = self.backend.get_or_create_user(access_token, None, user_info)
        except SuspiciousOperation as exc:
            logger.info("Core API resource server login failed: %s", exc)
            return None

        return user if user is not None and user.is_active else None
