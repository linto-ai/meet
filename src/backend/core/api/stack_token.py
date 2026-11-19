"""Exchange the OIDC id token of the session for a Twake stack access token."""

import ipaddress
import logging

import requests
from rest_framework import permissions, response, status, views

from core.models import User
from core.services.twake_drive import clean_instance

logger = logging.getLogger(__name__)

EXCHANGE_TIMEOUT = 5
# The stack refuses the id token: the browser must sign in again.
REJECTED_STATUSES = (400, 401, 403)


def public_hostname(value):
    """`value` as a bare public FQDN, or None (IP literal, localhost, one label)."""
    host = clean_instance(value)
    if not host or "." not in host or host == "localhost":
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    return None


class StackTokenView(views.APIView):
    """POST: the stack access token of the user's Twake instance."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        """Exchange the session id token with the user's stack."""
        id_token = request.session.get("oidc_id_token")
        if not id_token:
            return response.Response(
                {"error": "no_id_token"}, status=status.HTTP_401_UNAUTHORIZED
            )

        # Re-read: the stored value is untrusted, never the cached request.user.
        stored = (
            User.objects.filter(pk=request.user.pk)
            .values_list("twake_instance", flat=True)
            .first()
        )
        if not stored:
            return response.Response(
                {"error": "no_twake_instance"}, status=status.HTTP_409_CONFLICT
            )
        instance = public_hostname(stored)
        if not instance:
            return response.Response(
                {"error": "invalid_twake_instance"}, status=status.HTTP_400_BAD_REQUEST
            )

        failed = response.Response(
            {"error": "stack_exchange_failed"}, status=status.HTTP_502_BAD_GATEWAY
        )
        rejected = response.Response(
            {"error": "id_token_rejected"}, status=status.HTTP_401_UNAUTHORIZED
        )
        try:
            answer = requests.post(
                f"https://{instance}/auth/token_exchange",
                json={"id_token": id_token, "exchange_type": "app"},
                headers={"Accept": "application/json"},
                timeout=EXCHANGE_TIMEOUT,
                allow_redirects=False,
            )
            answer.raise_for_status()
            access_token = answer.json()["access_token"]
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            logger.warning("Stack token exchange failed for %s", instance)
            code = getattr(getattr(exc, "response", None), "status_code", None)
            return rejected if code in REJECTED_STATUSES else failed
        if not access_token or not isinstance(access_token, str):
            return failed

        return response.Response(
            {"access_token": access_token, "platform_url": f"https://{instance}"}
        )
