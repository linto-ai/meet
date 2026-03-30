"""Tasks related to LinTO Studio transcription."""

import logging

from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.core.files.storage import default_storage

from core import models
from core.tasks._task import task

logger = logging.getLogger(__name__)


@task
def process_linto_transcription(recording_id):
    """Upload recording to LinTO Studio and notify user when done.

    Downloads the audio file from MinIO, uploads it to LinTO Studio
    via the SDK, waits for transcription completion, optionally triggers
    an LLM summary, then shares the conversation with room participants.
    LinTO Studio handles email notifications automatically.
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

    # 5.5 Trigger LLM summary (failure does not block sharing)
    if getattr(settings, "LINTO_LLM_SUMMARY_ENABLED", True):
        try:
            services = await linto.list_llm_services()
            if services:
                service_route = (
                    getattr(settings, "LINTO_LLM_SERVICE_ROUTE", None)
                    or services[0].get("route")
                    or services[0].get("name")
                )
                conversation_id = media.response.get("_id", "")
                logger.info(
                    "Triggering LLM summary for %s (service=%s)",
                    recording_id,
                    service_route,
                )

                summary_handle = await linto.summarize(
                    conversation_id, service_route
                )

                summary_done = asyncio.Event()
                summary_result = {}

                def on_summary_done(content):
                    summary_result["success"] = True
                    summary_done.set()

                def on_summary_error(*args):
                    summary_result["success"] = False
                    summary_done.set()

                def on_summary_update(export):
                    status = export.get("status") if export else "unknown"
                    logger.info(
                        "LLM summary %s: %s", recording_id, status
                    )

                summary_handle.on("done", on_summary_done)
                summary_handle.on("error", on_summary_error)
                summary_handle.on("update", on_summary_update)

                timeout = getattr(
                    settings, "LINTO_LLM_SUMMARY_TIMEOUT", 600
                )
                await asyncio.wait_for(summary_done.wait(), timeout=timeout)

                if summary_result.get("success"):
                    logger.info(
                        "LLM summary done for %s", recording_id
                    )
                else:
                    logger.warning(
                        "LLM summary failed for %s, continuing",
                        recording_id,
                    )
            else:
                logger.info(
                    "No LLM services available, skipping summary for %s",
                    recording_id,
                )
        except Exception:
            logger.exception(
                "LLM summary error for %s, continuing", recording_id
            )

    # 6. Share conversation with room participants via LinTO Studio
    #    LinTO Studio sends email notifications automatically (with magic link)
    conversation_id = media.response.get("_id", "")
    room_emails = await sync_to_async(
        lambda: list(
            recording.room.accesses.select_related("user")
            .exclude(user__email__isnull=True)
            .exclude(user__email="")
            .values_list("user__email", flat=True)
        )
    )()

    for email in room_emails:
        try:
            await linto.share_conversation(conversation_id, email)
            logger.info(
                "Shared LinTO conversation %s with %s",
                conversation_id,
                email,
            )
        except Exception:
            logger.warning(
                "Failed to share conversation with %s, continuing",
                email,
                exc_info=True,
            )
