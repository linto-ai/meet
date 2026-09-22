"""LinTO capabilities of a participant (fork of the entitlements system).

The upstream entitlements interface answers ``{"can_create": bool}``; the
fork's ``linto`` backend adds a ``linto`` dict, the per-feature capabilities of
the person as LinTO Studio resolves them. This module is the ONE place that
decides what the front and the server gates read:

- ``None``: LinTO is not deployed here (``LINTO_FEATURE_ENABLED`` and
  ``LINTO_STUDIO_ENABLED`` both off), or the configured backend knows nothing
  about LinTO (upstream's ``local`` backend): no AI button at all;
- everything True when the per-user feature system is switched off
  (``LINTO_ENTITLEMENTS_ENABLED=False``);
- else what the backend answers, absent keys being False.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# The keys the front and the gates read. `quickMeeting` is Studio's own name
# for "may start a live transcription" (= transcription.live).
FEATURE_KEYS = ("summary", "translation", "recording")


def _capabilities(value):
    return {
        "quickMeeting": value,
        "transcription": {"live": value, "async": value},
        **dict.fromkeys(FEATURE_KEYS, value),
    }


def all_capabilities():
    """Every feature granted (kill switch off, or a service-account instance)."""
    return _capabilities(True)


def no_capabilities():
    """Nothing granted (no entitlement, or Studio unreachable)."""
    return _capabilities(False)


def normalize_capabilities(raw):
    """Studio's ``capabilities`` object → the fixed shape above.

    Unknown keys are kept (they come from the entitlement's ``features``, an
    extensible object), the known ones are coerced to booleans.
    """
    raw = raw if isinstance(raw, dict) else {}
    transcription = raw.get("transcription")
    transcription = transcription if isinstance(transcription, dict) else {}
    live = transcription.get("live") is True
    result = {
        **raw,
        "transcription": {
            **transcription,
            "live": live,
            "async": transcription.get("async") is True,
        },
        # Absent from Studio's answer only on an older build: derive it.
        "quickMeeting": raw.get("quickMeeting", live) is True,
    }
    for key in FEATURE_KEYS:
        result[key] = raw.get(key) is True
    return result


def user_linto_capabilities(user):
    """The LinTO capabilities of ``user`` (a Meet user), or None when nothing
    decides them here (see the module docstring). Anonymous → nothing."""
    if not (settings.LINTO_FEATURE_ENABLED or settings.LINTO_STUDIO_ENABLED):
        return None
    if not settings.LINTO_ENTITLEMENTS_ENABLED:
        return all_capabilities()
    if user is None or not getattr(user, "is_authenticated", False):
        return no_capabilities()
    # Local import: the package __init__ imports the factory, which reads the
    # settings; keep this module importable from anywhere.
    from core.entitlements import (  # noqa: PLC0415
        EntitlementsUnavailableError,
        get_user_entitlements,
    )

    try:
        entitlements = get_user_entitlements(user.sub, user.email)
    except EntitlementsUnavailableError:
        logger.warning("entitlements unavailable for user %s", user.id)
        return no_capabilities()
    capabilities = entitlements.get("linto")
    if capabilities is None:
        return None
    return normalize_capabilities(capabilities)


def has_linto_capability(user, path):
    """``has_linto_capability(user, "transcription.async")`` — False when the
    capabilities are not decided here (None) or the key is absent."""
    node = user_linto_capabilities(user)
    for part in path.split("."):
        if not isinstance(node, dict):
            return False
        node = node.get(part)
    return node is True


def recording_entitled(user):
    """May ``user`` start a video recording? Always, unless the instance gates
    the recording (``LINTO_RECORDING_ENTITLEMENT_ENABLED``), in which case the
    ``recording`` capability is required."""
    if not settings.LINTO_RECORDING_ENTITLEMENT_ENABLED:
        return True
    return has_linto_capability(user, "recording")
