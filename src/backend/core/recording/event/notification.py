"""Service to notify external services when a new recording is ready."""

import logging
import smtplib

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.translation import get_language, override
from django.utils.translation import gettext_lazy as _

import requests

from core import models
from core.tasks.linto import process_linto_transcription
from core.tasks.recording import process_screen_recording_to_twake

logger = logging.getLogger(__name__)


def _is_twake_configured() -> bool:
    """Return True when both Cloudery URL and token are set."""
    return bool(
        getattr(settings, "CLOUDERY_URL", None)
        and getattr(settings, "CLOUDERY_TOKEN", None)
    )


def get_recording_download_base_url() -> str:
    """Get the recording download base URL with backward compatibility."""
    new_setting = settings.RECORDING_DOWNLOAD_BASE_URL
    old_setting = settings.SCREEN_RECORDING_BASE_URL

    if old_setting:
        logger.warning(
            "SCREEN_RECORDING_BASE_URL is deprecated and will be removed in a future version. "
            "Please use RECORDING_DOWNLOAD_BASE_URL instead."
        )

    if new_setting:
        return new_setting

    return old_setting


class NotificationService:
    """Service for processing recordings and notifying external services."""

    def notify_and_update_status(self, recording) -> bool:
        """Notify external services then persist the resulting recording status.

        Centralizes the post-notification status transition shared by the
        storage-hook webhook and the polling fallback.
        """
        succeeded = self.notify_external_services(recording)
        recording.status = (
            models.RecordingStatusChoices.NOTIFICATION_SUCCEEDED
            if succeeded
            else models.RecordingStatusChoices.SAVED
        )
        recording.save(update_fields=["status", "updated_at"])
        return succeeded

    def notify_external_services(self, recording):
        """Process a recording based on its mode.

        Routing:
        - mode=TRANSCRIPT → LinTO (or summary service fallback)
        - mode=SCREEN_RECORDING + transcribe=True → LinTO handles video+audio+email
        - mode=SCREEN_RECORDING + transcribe=False + Twake configured → upload MP4 to Twake + email
        - mode=SCREEN_RECORDING + transcribe=False + Twake absent → direct download email only
        """

        if recording.mode == models.RecordingModeChoices.TRANSCRIPT:
            if getattr(settings, "LINTO_STUDIO_ENABLED", False):
                return self._notify_linto_studio(recording)
            return self._notify_summary_service(recording)

        if recording.mode == models.RecordingModeChoices.SCREEN_RECORDING:
            if recording.options.get("transcribe", False):
                if getattr(settings, "LINTO_STUDIO_ENABLED", False):
                    return self._notify_linto_studio(recording)
                summary_success = self._notify_summary_service(recording)
                email_success = self._notify_user_by_email(recording)
                return email_success and summary_success

            if _is_twake_configured():
                return self._notify_screen_recording_to_twake(recording)

            return self._notify_user_by_email(recording)

        logger.error(
            "Unknown recording mode %s for recording %s",
            recording.mode,
            recording.id,
        )
        return False

    @staticmethod
    def _notify_screen_recording_to_twake(recording) -> bool:
        """Launch async Twake Drive upload via Celery task."""
        try:
            process_screen_recording_to_twake.delay(str(recording.id))
            return True
        except Exception:
            logger.exception(
                "Failed to queue Twake upload task for recording %s",
                recording.id,
            )
            return False

    @staticmethod
    def _notify_user_by_email(recording) -> bool:
        """
        Send an email notification to recording owners when their recording is ready.

        The email includes a direct link that redirects owners to a dedicated download
        page in the frontend where they can access their specific recording.
        """

        owner_accesses = (
            models.RecordingAccess.objects.select_related("user")
            .filter(
                role=models.RoleChoices.OWNER,
                recording_id=recording.id,
            )
            .order_by("created_at")
        )

        if not owner_accesses:
            logger.error("No owner found for recording %s", recording.id)
            return False

        context = {
            "brandname": settings.EMAIL_BRAND_NAME,
            "support_email": settings.EMAIL_SUPPORT_EMAIL,
            "logo_img": settings.EMAIL_LOGO_IMG,
            "domain": settings.EMAIL_DOMAIN,
            "room_name": recording.room.name,
            "recording_expiration_days": settings.RECORDING_EXPIRATION_DAYS,
            "link": f"{get_recording_download_base_url()}/{recording.id}",
        }

        has_failures = False

        # We process emails individually rather than in batch because:
        # 1. Each email requires personalization (timezone, language)
        # 2. The number of recipients per recording is typically small (not thousands)
        for access in owner_accesses:
            user = access.user
            language = user.language or get_language()
            with override(language):
                personalized_context = {
                    "recording_date": recording.created_at.astimezone(
                        user.timezone
                    ).strftime("%Y-%m-%d"),
                    "recording_time": recording.created_at.astimezone(
                        user.timezone
                    ).strftime("%H:%M"),
                    **context,
                }
                msg_html = render_to_string(
                    "mail/html/screen_recording.html", personalized_context
                )
                msg_plain = render_to_string(
                    "mail/text/screen_recording.txt", personalized_context
                )
                subject = str(_("Your recording is ready"))  # Force translation

                try:
                    send_mail(
                        subject.capitalize(),
                        msg_plain,
                        settings.EMAIL_FROM,
                        [user.email],
                        html_message=msg_html,
                        fail_silently=False,
                    )
                except smtplib.SMTPException as exception:
                    logger.error("notification could not be sent: %s", exception)
                    has_failures = True

        return not has_failures

    @staticmethod
    def _notify_summary_service(recording):
        """Notify summary service about a new recording."""

        if (
            not settings.SUMMARY_SERVICE_ENDPOINT
            or not settings.SUMMARY_SERVICE_API_TOKEN
        ):
            logger.error("Summary service not configured")
            return False

        owner_access = (
            models.RecordingAccess.objects.select_related("user")
            .filter(
                role=models.RoleChoices.OWNER,
                recording_id=recording.id,
            )
            .first()
        )

        if not owner_access:
            logger.error("No owner found for recording %s", recording.id)
            return False
        payload = {
            "owner_id": str(owner_access.user.id),
            "filename": recording.key,
            "email": owner_access.user.email,
            "sub": owner_access.user.sub,
            "room": recording.room.name,
            "language": recording.options.get("language"),
            "recording_date": recording.created_at.astimezone(
                owner_access.user.timezone
            ).strftime("%Y-%m-%d"),
            "recording_time": recording.created_at.astimezone(
                owner_access.user.timezone
            ).strftime("%H:%M"),
            "download_link": f"{get_recording_download_base_url()}/{recording.id}",
            "context_language": owner_access.user.language,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.SUMMARY_SERVICE_API_TOKEN}",
        }

        try:
            response = requests.post(
                settings.SUMMARY_SERVICE_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.exception(
                "Summary service error for recording %s. URL: %s. Exception: %s",
                recording.id,
                settings.SUMMARY_SERVICE_ENDPOINT,
                exc,
            )
            return False

        return True

    @staticmethod
    def _notify_linto_studio(recording) -> bool:
        """Launch async LinTO Studio transcription via Celery task."""
        try:
            process_linto_transcription.delay(str(recording.id))
            return True
        except Exception:
            logger.exception(
                "Failed to queue LinTO Studio task for recording %s",
                recording.id,
            )
            return False


notification_service = NotificationService()
