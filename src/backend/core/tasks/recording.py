"""Tasks related to screen recording upload to Twake Drive."""

import logging
import smtplib

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.translation import get_language, override
from django.utils.translation import gettext_lazy as _

from asgiref.sync import async_to_sync, sync_to_async

from core import models
from core.services.twake_recording import upload_recording_files
from core.tasks._task import task

logger = logging.getLogger(__name__)


@task
def process_screen_recording_to_twake(recording_id):
    """Upload a screen recording (MP4) to Twake Drive and notify owners.

    Downloads the MP4 from MinIO, uploads it to Twake Drive under
    `_Reunions/Reunion_{date}/Enregistrement_{date}.mp4`, then sends an
    email to every owner with the direct download link plus the Twake
    Drive link when available.
    """
    try:
        _process_screen_recording_sync(recording_id)
    except Exception:
        logger.exception(
            "Screen recording Twake upload failed for recording %s",
            recording_id,
        )


@async_to_sync
async def _process_screen_recording_sync(recording_id):
    """Async implementation: download MP4, upload Twake, email owners."""
    from core.recording.event.notification import get_recording_download_base_url

    recording = await sync_to_async(
        models.Recording.objects.select_related("room").get
    )(id=recording_id)

    logger.info("Downloading %s from MinIO", recording.key)

    @sync_to_async
    def download_from_minio():
        s3_client = default_storage.connection.meta.client
        s3_response = s3_client.get_object(
            Bucket=default_storage.bucket_name,
            Key=recording.key,
        )
        return s3_response["Body"].read()

    file_content = await download_from_minio()
    logger.info(
        "Downloaded %d bytes for recording %s",
        len(file_content),
        recording_id,
    )

    owner_access = await sync_to_async(
        lambda: (
            models.RecordingAccess.objects.filter(
                role=models.RoleChoices.OWNER,
                recording_id=recording.id,
            )
            .select_related("user")
            .order_by("created_at")
            .first()
        )
    )()

    if not owner_access:
        logger.error("No owner found for recording %s", recording_id)
        return

    meeting_time = recording.created_at.strftime("%d-%m-%Y_%H-%M")

    twake_drive_link = None
    try:
        twake_drive_link = await upload_recording_files(
            recording,
            owner_access,
            [
                {
                    "filename": f"Enregistrement_{meeting_time}.mp4",
                    "content": file_content,
                    "content_type": "video/mp4",
                }
            ],
        )
    except Exception:
        logger.exception(
            "Twake Drive upload failed for recording %s, continuing with email",
            recording_id,
        )

    download_base = get_recording_download_base_url()
    download_link = f"{download_base}/{recording.id}" if download_base else None

    room_name = recording.room.name or "Meeting"

    accesses = await sync_to_async(
        lambda: list(
            models.RecordingAccess.objects.filter(
                role=models.RoleChoices.OWNER,
                recording_id=recording.id,
            )
            .select_related("user")
            .exclude(user__email__isnull=True)
            .exclude(user__email="")
        )
    )()

    for access in accesses:
        user = access.user
        try:

            @sync_to_async
            def _send_email(user=user):
                language = user.language or get_language()
                with override(language):
                    ctx = {
                        "brandname": settings.EMAIL_BRAND_NAME,
                        "support_email": settings.EMAIL_SUPPORT_EMAIL,
                        "logo_img": settings.EMAIL_LOGO_IMG,
                        "domain": settings.EMAIL_DOMAIN,
                        "room_name": room_name,
                        "recording_expiration_days": (
                            settings.RECORDING_EXPIRATION_DAYS
                        ),
                        "recording_date": recording.created_at.astimezone(
                            user.timezone
                        ).strftime("%Y-%m-%d"),
                        "recording_time": recording.created_at.astimezone(
                            user.timezone
                        ).strftime("%H:%M"),
                        "link": download_link,
                        "twake_drive_link": twake_drive_link,
                    }
                    msg_html = render_to_string("mail/html/screen_recording.html", ctx)
                    msg_plain = render_to_string("mail/text/screen_recording.txt", ctx)
                    subject = str(_("Your recording is ready"))
                    email_msg = EmailMultiAlternatives(
                        subject.capitalize(),
                        msg_plain,
                        settings.EMAIL_FROM,
                        [user.email],
                    )
                    email_msg.attach_alternative(msg_html, "text/html")
                    email_msg.send(fail_silently=False)

            await _send_email()
            logger.info(
                "Sent screen recording email to %s for %s",
                user.email,
                recording_id,
            )
        except smtplib.SMTPException:
            logger.warning(
                "Failed to send screen recording email to %s",
                user.email,
                exc_info=True,
            )
