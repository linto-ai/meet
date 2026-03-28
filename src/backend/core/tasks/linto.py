"""Tasks related to LinTO Studio transcription."""

import logging
import smtplib

from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.utils.translation import override

from core import models
from core.tasks._task import task

logger = logging.getLogger(__name__)


@task
def process_linto_transcription(recording_id):
    """Upload recording to LinTO Studio and notify user when done.

    Downloads the audio file from MinIO, uploads it to LinTO Studio
    via the SDK, waits for transcription completion, then sends
    an email to the recording owner with a preview and link.
    """
    try:
        _process_linto_transcription_sync(recording_id)
    except Exception:
        logger.exception(
            "LinTO Studio transcription failed for recording %s",
            recording_id,
        )


@async_to_sync
async def _process_linto_transcription_sync(recording_id):
    """Async implementation using LinTO SDK."""
    import asyncio

    from linto import LinTO

    recording = await sync_to_async(
        models.Recording.objects.select_related("room").get
    )(id=recording_id)

    # 1. Download audio from MinIO
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

    # 2. Initialize SDK (auto-discovers org, auto-selects ASR service)
    linto = LinTO(
        auth_token=settings.LINTO_STUDIO_API_TOKEN,
        base_url=settings.LINTO_STUDIO_BASE_URL,
    )

    # 3. Upload + start transcription (SDK handles config generation)
    language = recording.options.get("language") or "*"
    room_name = recording.room.name or "Meeting"
    date_str = recording.created_at.strftime("%Y-%m-%d %H:%M")

    logger.info(
        "Uploading recording %s to LinTO Studio (lang=%s)",
        recording_id,
        language,
    )

    handle = await linto.transcribe(
        file=file_content,
        enable_diarization=True,
        number_of_speaker="0",
        language=language,
        enablePunctuation=True,
        name=f"{room_name} - {date_str}",
    )

    # 4. Wait for transcription via SDK polling (1s interval)
    done_event = asyncio.Event()
    result = {}

    def on_done(media):
        result["media"] = media
        result["success"] = True
        done_event.set()

    def on_error(*args):
        result["success"] = False
        done_event.set()

    def on_update(job):
        state = job.get("state") if job else "unknown"
        logger.info("LinTO transcription %s: %s", recording_id, state)

    handle.on("done", on_done)
    handle.on("error", on_error)
    handle.on("update", on_update)

    await asyncio.wait_for(done_event.wait(), timeout=3600)

    if not result.get("success"):
        raise RuntimeError(
            f"LinTO Studio transcription failed for recording {recording_id}"
        )

    # 5. Exploit SDK Media object
    media = result["media"]
    logger.info(
        "Transcription done for %s: %d turns, %d chars",
        recording_id,
        len(media.turns),
        len(media.full_text),
    )

    # 6. Send email with preview + link
    await sync_to_async(_send_linto_result_email)(recording, media)


def _send_linto_result_email(recording, media):
    """Send email to recording owner with transcription preview."""
    owner_access = (
        models.RecordingAccess.objects.select_related("user")
        .filter(
            role=models.RoleChoices.OWNER,
            recording_id=recording.id,
        )
        .first()
    )
    if not owner_access:
        logger.warning(
            "No owner found for recording %s, skipping email",
            recording.id,
        )
        return

    user = owner_access.user

    # SDK Media: formatted preview with speaker names and timestamps
    preview = media.to_format(
        sep=" - ",
        meta_text_sep=" : ",
        eol="LF",
        include={"speaker": True, "lang": False, "timestamp": True},
        order=["speaker", "timestamp"],
    )
    if len(preview) > 1000:
        preview = preview[:1000] + "\n..."

    # SDK Media: extract unique speaker names
    speakers = sorted(
        {t["speaker"] for t in media.turns if t.get("speaker")}
    )

    # Build LinTO Studio conversation URL
    # Frontend route: /interface/{orgId}/conversations/{convId}
    conversation_id = media.response.get("_id", "")
    org_id = media.response.get("organization", {})
    if isinstance(org_id, dict):
        org_id = org_id.get("organizationId", "")
    linto_base = (settings.LINTO_STUDIO_BASE_URL or "").replace(
        "/cm-api", ""
    )
    conversation_url = (
        f"{linto_base}/interface/{org_id}/conversations/{conversation_id}"
    )

    language = user.language or "en"
    with override(language):
        try:
            send_mail(
                subject=f"Transcription : {recording.room.name}",
                message=(
                    f"Votre transcription est prête.\n\n"
                    f"Réunion : {recording.room.name}\n"
                    f"Date : {recording.created_at.strftime('%Y-%m-%d %H:%M')}\n"
                    f"Participants : {', '.join(speakers) or 'N/A'}\n"
                    f"Segments : {len(media.turns)}\n\n"
                    f"--- Aperçu ---\n{preview}\n\n"
                    f"Transcription complète :\n{conversation_url}\n"
                ),
                from_email=settings.EMAIL_FROM,
                recipient_list=[user.email],
                fail_silently=False,
            )
            logger.info(
                "LinTO result email sent to %s for recording %s",
                user.email,
                recording.id,
            )
        except smtplib.SMTPException:
            logger.exception(
                "Failed to send LinTO result email for recording %s",
                recording.id,
            )
