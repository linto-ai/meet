"""LinTO Studio entitlements backend: what a person may do with LinTO.

Studio holds the rights (declared by the external subscription system through
its entitlements API) and answers ``POST /api/auth/external/resolve`` with the
capabilities of an identity — with NO side effect: no key, no token. That is
the only route this backend calls; the key of the person is born later, when
they start a transcription (``BotTranscriptionService.studio_token_for``).
"""

import logging

from django.conf import settings
from django.core.cache import cache

import requests

from core.entitlements.backends.base import EntitlementsBackend
from core.entitlements.capabilities import (
    all_capabilities,
    no_capabilities,
    normalize_capabilities,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10


class LintoEntitlementsBackend(EntitlementsBackend):
    """Backend that reads the capabilities from LinTO Studio's resolution.

    ``get_user_entitlements`` returns ``{"can_create": True, "linto": {...}}``:
    room creation never depends on Studio, ``linto`` is the capabilities dict
    (``transcription.{live,async}``, ``summary``, ``translation``,
    ``recording``, ``quickMeeting``) — all False when Studio holds nothing for
    that person, or when it cannot be reached and no cached answer exists.
    Answers are cached per user for ``ENTITLEMENTS_CACHE_TIMEOUT`` (display
    only: Studio re-reads the entitlement at every gated call). With
    ``LINTO_ENTITLEMENTS_ENABLED=False`` everything is granted without a call.

    Args:
        timeout: HTTP request timeout in seconds.
    """

    def __init__(self, *, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout

    @staticmethod
    def _cache_key(user_sub):
        return f"entitlements:linto:{settings.LINTO_IDENTITY_PROVIDER}:{user_sub}"

    def _resolve(self, user_sub, user_email):
        """One call to Studio. Returns the capabilities, or None when Studio
        could not answer (network, 5xx, bad credentials)."""
        # Local import: the service module imports core.api, which imports us.
        from core.services.bot_transcription import (  # noqa: PLC0415
            BotTranscriptionException,
            BotTranscriptionService,
        )

        payload = {"provider": settings.LINTO_IDENTITY_PROVIDER, "subject": user_sub}
        email = (user_email or "").strip().lower()
        if email:
            payload["email"] = email
        try:
            service = BotTranscriptionService()
            response = requests.post(
                f"{service.studio_base}/api/auth/external/resolve",
                # Harmless for an integration key; required when the credential
                # is a system administrator (dev / transition).
                params={"userScope": "backoffice"},
                json=payload,
                headers=service._headers(),  # noqa: SLF001
                timeout=self.timeout,
            )
        except (requests.RequestException, BotTranscriptionException) as exc:
            logger.warning("LinTO entitlement resolution error: %s", exc)
            return None

        if response.status_code == 200:
            data = response.json() or {}
            return normalize_capabilities(data.get("capabilities"))
        if response.status_code == 404:
            # Nothing declared for that identity, or every feature off.
            return no_capabilities()
        logger.warning(
            "LinTO entitlement resolution failed: %s %s",
            response.status_code,
            response.text[:200],
        )
        return None

    def get_user_entitlements(
        self, user_sub, user_email, user_info=None, force_refresh=False
    ):
        """The entitlements of one person, cached.

        Never raises: a Studio outage falls back to the cached answer, else to
        "no LinTO capability" — room creation is never blocked by Studio.
        """
        if not settings.LINTO_ENTITLEMENTS_ENABLED:
            return {"can_create": True, "linto": all_capabilities()}

        cache_key = self._cache_key(user_sub)
        if not force_refresh:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        capabilities = self._resolve(user_sub, user_email)
        if capabilities is None:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
            return {"can_create": True, "linto": no_capabilities()}

        result = {"can_create": True, "linto": capabilities}
        cache.set(cache_key, result, settings.ENTITLEMENTS_CACHE_TIMEOUT)
        return result
