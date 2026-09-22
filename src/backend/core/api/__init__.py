"""Meet core API endpoints"""

from django.conf import settings
from django.core.exceptions import ValidationError

from rest_framework import exceptions as drf_exceptions
from rest_framework import views as drf_views
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.utils import build_telephony_config


def exception_handler(exc, context):
    """Handle Django ValidationError as an accepted exception.

    For the parameters, see ``exception_handler``
    This code comes from twidi's gist:
    https://gist.github.com/twidi/9d55486c36b6a51bdcb05ce3a763e79f
    """
    if isinstance(exc, ValidationError):
        if hasattr(exc, "message_dict"):
            detail = exc.message_dict
        elif hasattr(exc, "message"):
            detail = exc.message
        elif hasattr(exc, "messages"):
            detail = exc.messages
        else:
            detail = ""

        exc = drf_exceptions.ValidationError(detail=detail)

    return drf_views.exception_handler(exc, context)


# pylint: disable=unused-argument
@api_view(["GET"])
def get_frontend_configuration(request):
    """Returns the frontend configuration dict as configured in settings."""
    # Local import: the service module imports core.api (permissions/serializers).
    from core.services.bot_transcription import (  # noqa: PLC0415
        effective_token_source,
    )

    frontend_configuration = {
        "LANGUAGE_CODE": settings.LANGUAGE_CODE,
        "recording": {
            "is_enabled": settings.RECORDING_ENABLE,
            "available_modes": settings.RECORDING_WORKER_CLASSES.keys(),
            "expiration_days": settings.RECORDING_EXPIRATION_DAYS,
            "max_duration": settings.RECORDING_MAX_DURATION,
            "screen_recording_permission": settings.RECORDING_SCREEN_PERMISSION,
            "transcript_permission": settings.RECORDING_TRANSCRIPT_PERMISSION,
        },
        "background_image": {
            "upload_is_enabled": settings.FILE_UPLOAD_ENABLED,
            "max_count_by_user": settings.FILE_UPLOAD_RESTRICTIONS["background_image"][
                "max_count_by_user"
            ],
            "max_size": settings.FILE_UPLOAD_RESTRICTIONS["background_image"][
                "max_size"
            ],
            "allowed_extensions": settings.FILE_UPLOAD_RESTRICTIONS["background_image"][
                "allowed_extensions"
            ],
            "allowed_mimetypes": settings.FILE_UPLOAD_RESTRICTIONS["background_image"][
                "allowed_mimetypes"
            ],
        },
        "telephony": build_telephony_config(),
        "resource": {
            "default_access_level": settings.RESOURCE_DEFAULT_ACCESS_LEVEL,
        },
        "subtitle": {"enabled": settings.ROOM_SUBTITLE_ENABLED},
        # LinTO live transcription (fork): the in-meeting "LinTO" tool. Browser-
        # first — the panel talks to studio-api via the JS SDK as the user, and
        # only mints the native bot join token + lifecycle hooks via the Meet
        # backend. `studio_api_url` is the browser-facing studio-api base.
        "linto": {
            "enabled": settings.LINTO_FEATURE_ENABLED,
            "hide_legacy_tools": settings.LINTO_HIDE_LEGACY_TOOLS,
            "studio_api_url": settings.LINTO_STUDIO_BROWSER_API_URL,
            # Where the browser's Studio JWT comes from (informational: the
            # panel always asks GET rooms/{id}/linto/studio-token). Reported
            # AFTER the kill switch below: the service account when it is off.
            "token_source": effective_token_source(),
            # Per-user feature gating. False = every capability granted to
            # everyone, no entitlement lookup (LINTO_ENTITLEMENTS_ENABLED).
            "entitlements_enabled": settings.LINTO_ENTITLEMENTS_ENABLED,
            # The video recording requires the `recording` capability
            # (LINTO_RECORDING_ENTITLEMENT_ENABLED); False = open to everyone.
            "recording_entitlement_enabled": (
                settings.LINTO_RECORDING_ENTITLEMENT_ENABLED
            ),
            "native_livekit_url": settings.LINTO_NATIVE_LIVEKIT_URL,
            "visio_native_enabled": settings.LINTO_VISIO_NATIVE_ENABLED,
            "bot_provider": settings.LINTO_BOT_PROVIDER,
            # Pinned ASR profile — the panel never lets the user pick one.
            "default_profile_id": settings.LINTO_STUDIO_DEFAULT_PROFILE_ID,
        },
        "diagnostics": {"connection_test_enabled": settings.CONNECTION_TEST_ENABLED},
        "livekit": {
            "url": settings.LIVEKIT_CONFIGURATION["url"],
            "force_wss_protocol": settings.LIVEKIT_FORCE_WSS_PROTOCOL,
            "enable_firefox_proxy_workaround": settings.LIVEKIT_ENABLE_FIREFOX_PROXY_WORKAROUND,
            "default_sources": settings.LIVEKIT_DEFAULT_SOURCES,
        },
        "authenticated_users_can_edit_display_name": (
            settings.AUTHENTICATED_PARTICIPANTS_CAN_EDIT_DISPLAY_NAME
        ),
    }
    frontend_configuration.update(settings.FRONTEND_CONFIGURATION)
    return Response(frontend_configuration)
