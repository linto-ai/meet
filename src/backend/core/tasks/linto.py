"""Tasks related to LinTO Studio transcription."""

import logging
import smtplib

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.translation import get_language, override
from django.utils.translation import gettext_lazy as _

import bleach
import markdown as markdown_lib
from asgiref.sync import async_to_sync, sync_to_async

from core import models
from core.recording.enums import FileExtension
from core.services.audio_extract import extract_audio_from_video
from core.tasks._task import task

logger = logging.getLogger(__name__)

_SUMMARY_HTML_TAGS = [
    "p",
    "br",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "code",
    "pre",
    "a",
]
_SUMMARY_HTML_ATTRS = {"a": ["href", "title"]}


def _render_summary_html(text):
    """Render LinTO summary markdown to a sanitized HTML fragment."""
    if not text:
        return None
    html = markdown_lib.markdown(text, extensions=["extra", "sane_lists", "nl2br"])
    return bleach.clean(
        html,
        tags=_SUMMARY_HTML_TAGS,
        attributes=_SUMMARY_HTML_ATTRS,
        strip=True,
    )


@task
def process_linto_transcription(recording_id):
    """Upload recording to LinTO Studio and notify user when done.

    Downloads the audio file from MinIO, uploads it to LinTO Studio
    via the SDK, waits for transcription completion, optionally triggers
    an LLM summary, generates a PDF via publication templates, then
    sends an email with the PDF to room participants.
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

    # 1.5 Extract audio if source is a video (LinTO only accepts audio).
    # Stream-copy the original AAC track into an .m4a container — no
    # re-encoding, lossless, fast; LinTO handles resampling internally.
    if recording.extension == FileExtension.MP4.value:
        logger.info(
            "Demuxing audio from MP4 for LinTO for recording %s",
            recording_id,
        )
        audio_content = await sync_to_async(extract_audio_from_video)(
            file_content, output_format="copy"
        )
    else:
        audio_content = file_content

    # 2. Initialize SDK (auto-discovers org, auto-selects ASR service)
    linto = LinTO(
        auth_token=settings.LINTO_STUDIO_API_TOKEN,
        base_url=settings.LINTO_STUDIO_BASE_URL,
    )

    # 3. Upload + start transcription (SDK handles config generation)
    language = recording.options.get("language") or "*"
    meeting_time = recording.created_at.strftime("%d-%m-%Y_%H-%M")
    conversation_name = f"Reunion_{meeting_time}"

    logger.info(
        "Uploading recording %s to LinTO Studio (lang=%s)",
        recording_id,
        language,
    )

    try:
        handle = await linto.transcribe(
            file=audio_content,
            enable_diarization=True,
            number_of_speaker="0",
            language=language,
            enablePunctuation=True,
            name=conversation_name,
            members_right=getattr(settings, "LINTO_STUDIO_MEMBERS_RIGHT", 0),
        )
    except RuntimeError as exc:
        logger.error(
            "LinTO ASR configuration error for recording %s (lang=%s): %s",
            recording_id,
            language,
            exc,
        )
        raise

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

    conversation_id = media.response.get("_id", "")

    # 5.1 Tag the conversation as Visio (failure does not block)
    if conversation_id:
        try:
            tag_id = await linto.ensure_tag(
                name=settings.LINTO_VISIO_TAG_NAME,
                category_name=settings.LINTO_VISIO_TAG_CATEGORY,
            )
            if tag_id:
                await linto.add_conversation_tag(conversation_id, tag_id)
                logger.info(
                    "Tagged conversation %s with %s for %s",
                    conversation_id,
                    settings.LINTO_VISIO_TAG_NAME,
                    recording_id,
                )
            else:
                logger.warning(
                    "No tag id resolved for %s, skipping tag",
                    recording_id,
                )
        except Exception:
            logger.exception("Tag conversation error for %s, continuing", recording_id)

    # 5.2 Move the conversation into the Visio folder (failure does not block)
    if conversation_id:
        try:
            folder_id = await linto.ensure_folder(
                name=settings.LINTO_VISIO_FOLDER_NAME,
                visibility="private",
            )
            if folder_id:
                await linto.move_to_folder(folder_id, conversation_id)
                logger.info(
                    "Moved conversation %s into folder %s for %s",
                    conversation_id,
                    settings.LINTO_VISIO_FOLDER_NAME,
                    recording_id,
                )
            else:
                logger.warning(
                    "No folder id resolved for %s, skipping move",
                    recording_id,
                )
        except Exception:
            logger.exception("Move conversation error for %s, continuing", recording_id)

    # 5.5 Trigger LLM summary (failure does not block email)
    summary_result = {}
    if getattr(settings, "LINTO_LLM_SUMMARY_ENABLED", True):
        try:
            services = await linto.list_llm_services()
            if services:
                service_route = (
                    getattr(settings, "LINTO_LLM_SERVICE_ROUTE", None)
                    or services[0].get("route")
                    or services[0].get("name")
                )
                logger.info(
                    "Triggering LLM summary for %s (service=%s)",
                    recording_id,
                    service_route,
                )

                summary_handle = await linto.summarize(conversation_id, service_route)

                summary_done = asyncio.Event()
                summary_result = {}

                def on_summary_done(result):
                    summary_result["success"] = True
                    if isinstance(result, dict):
                        summary_result["content"] = result.get("content")
                        summary_result["job_id"] = result.get("jobId")
                    else:
                        summary_result["content"] = result
                    summary_done.set()

                def on_summary_error(*args):
                    summary_result["success"] = False
                    summary_done.set()

                def on_summary_update(export):
                    status = export.get("status") if export else "unknown"
                    logger.info("LLM summary %s: %s", recording_id, status)

                summary_handle.on("done", on_summary_done)
                summary_handle.on("error", on_summary_error)
                summary_handle.on("update", on_summary_update)

                timeout = getattr(settings, "LINTO_LLM_SUMMARY_TIMEOUT", 600)
                await asyncio.wait_for(summary_done.wait(), timeout=timeout)

                if summary_result.get("success"):
                    logger.info("LLM summary done for %s", recording_id)
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
            logger.exception("LLM summary error for %s, continuing", recording_id)

    # 5.6 Generate document attachment (failure does not block email)
    pdf_content = None
    pub_format = getattr(settings, "LINTO_PUBLICATION_FORMAT", "pdf")

    if summary_result.get("success") and summary_result.get("job_id"):
        # Summary available: export via publication template
        try:
            job_id = summary_result["job_id"]
            template_id = getattr(settings, "LINTO_PUBLICATION_TEMPLATE_ID", None)
            if not template_id:
                result = await linto.get_publication_templates()
                templates = (
                    result.get("templates", result)
                    if isinstance(result, dict)
                    else result
                )
                if isinstance(templates, list) and templates:
                    default = next(
                        (t for t in templates if t.get("is_default")),
                        templates[0],
                    )
                    template_id = default.get("id") or default.get("_id")

            if template_id:
                logger.info(
                    "Exporting %s for %s (job=%s, template=%s)",
                    pub_format,
                    recording_id,
                    job_id,
                    template_id,
                )
                pdf_content = await linto.export_with_template(
                    job_id=job_id,
                    format=pub_format,
                    template_id=template_id,
                )
                logger.info(
                    "%s exported for %s: %d bytes",
                    pub_format.upper(),
                    recording_id,
                    len(pdf_content) if pdf_content else 0,
                )
            else:
                logger.warning(
                    "No publication template found for %s, skipping document",
                    recording_id,
                )
        except Exception:
            logger.exception(
                "Document generation error for %s, continuing", recording_id
            )

    elif conversation_id:
        # No summary: export transcription directly as DOCX
        try:
            download_format = pub_format if pub_format in ("docx", "odt") else "docx"
            logger.info(
                "Downloading transcription as %s for %s",
                download_format,
                recording_id,
            )
            pdf_content = await linto.download_conversation(
                conversation_id=conversation_id,
                format=download_format,
            )
            pub_format = download_format
            logger.info(
                "%s downloaded for %s: %d bytes",
                pub_format.upper(),
                recording_id,
                len(pdf_content) if pdf_content else 0,
            )
        except Exception:
            logger.exception(
                "Transcription download error for %s, continuing", recording_id
            )

    # Prepare publication filename and mime type
    room_name = recording.room.name or "Meeting"
    date_str = recording.created_at.strftime("%Y-%m-%d")
    mime_types = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "odt": "application/vnd.oasis.opendocument.text",
    }
    pub_mime = mime_types.get(pub_format, "application/octet-stream")
    pub_filename = f"transcription-{room_name}-{date_str}.{pub_format}"

    # Extract summary preview text (needed for Docs content and email)
    summary_preview = None
    if summary_result.get("content"):
        c = summary_result["content"]
        if isinstance(c, dict):
            summary_preview = c.get("text") or c.get("content") or str(c)
        elif isinstance(c, str):
            summary_preview = c

    # 5.8 Share conversation with the user who triggered the recording
    # (RecordingAccess role=OWNER is created for the trigger user in
    # start_room_recording — not necessarily a room admin).
    owner_access = await sync_to_async(
        lambda: recording.accesses.filter(role="owner").select_related("user").first()
    )()

    if owner_access and owner_access.user.email and conversation_id:
        try:
            await linto.share_conversation(
                conversation_id=conversation_id,
                email=owner_access.user.email,
                right=31,  # READ+COMMENT+WRITE+SHARE+DELETE (OWNER=32 is reserved)
                notify=False,  # meet/ sends its own recap email — skip LinTO's
            )
            logger.info(
                "Conversation shared with %s (right=31) for %s",
                owner_access.user.email,
                recording_id,
            )
        except Exception:
            logger.exception(
                "Share conversation error for %s, continuing", recording_id
            )

    # 5.7 Upload to Twake Drive via Cloudery (failure does not block email)
    twake_drive_link = None
    twake_configured = getattr(settings, "CLOUDERY_URL", None) and getattr(
        settings, "CLOUDERY_TOKEN", None
    )

    if twake_configured:
        try:
            from core.services.twake_drive import (
                build_drive_link,
                ensure_meeting_directory,
                get_drive_token,
                save_file,
            )

            if owner_access and owner_access.user.sub:
                sub = owner_access.user.sub
                domain = getattr(
                    settings, "TWAKE_INSTANCE_DOMAIN", "twake.linagora.com"
                )
                instance = (
                    getattr(settings, "TWAKE_DEV_INSTANCE_OVERRIDE", None)
                    or f"{sub}.{domain}"
                )

                drive_token = await get_drive_token(
                    cloudery_url=settings.CLOUDERY_URL,
                    cloudery_token=settings.CLOUDERY_TOKEN,
                    instance=instance,
                )

                meeting_time = recording.created_at.strftime("%d-%m-%Y_%H-%M")
                dirname = f"Reunion_{meeting_time}"
                dir_id = await ensure_meeting_directory(instance, drive_token, dirname)

                transcript_content = media.full_text or ""
                await save_file(
                    instance=instance,
                    token=drive_token,
                    dir_id=dir_id,
                    filename=f"Transcription_{meeting_time}.cozy-note",
                    content=transcript_content,
                    content_type="text/vnd.cozy.note+markdown",
                )

                if summary_preview:
                    await save_file(
                        instance=instance,
                        token=drive_token,
                        dir_id=dir_id,
                        filename="Résumé.cozy-note",
                        content=summary_preview,
                        content_type="text/vnd.cozy.note+markdown",
                    )

                # Upload original recording (video or audio)
                if recording.extension == FileExtension.MP4.value:
                    await save_file(
                        instance=instance,
                        token=drive_token,
                        dir_id=dir_id,
                        filename=f"Enregistrement_{meeting_time}.mp4",
                        content=file_content,
                        content_type="video/mp4",
                    )
                    # Also upload extracted audio alongside the video
                    # (OGG for drive — compressed, smaller; WAV was only for LinTO)
                    ogg_audio = await sync_to_async(extract_audio_from_video)(
                        file_content, output_format="ogg"
                    )
                    await save_file(
                        instance=instance,
                        token=drive_token,
                        dir_id=dir_id,
                        filename=f"Enregistrement_{meeting_time}.ogg",
                        content=ogg_audio,
                        content_type="audio/ogg",
                    )
                else:
                    await save_file(
                        instance=instance,
                        token=drive_token,
                        dir_id=dir_id,
                        filename=f"Enregistrement_{meeting_time}.ogg",
                        content=file_content,
                        content_type="audio/ogg",
                    )

                # Upload LinTO Studio shortcut
                frontend_url = getattr(settings, "LINTO_STUDIO_FRONTEND_URL", None)
                if frontend_url and conversation_id:
                    shortcut_url = (
                        f"{frontend_url}/interface/conversations/{conversation_id}"
                    )
                    shortcut_content = f"[InternetShortcut]\nURL={shortcut_url}\n"
                    await save_file(
                        instance=instance,
                        token=drive_token,
                        dir_id=dir_id,
                        filename="Plus_de_detail_dans_linto.url",
                        content=shortcut_content,
                        content_type="application/x-url",
                    )

                # Upload formatted document (PDF/DOCX from publication)
                if pdf_content:
                    await save_file(
                        instance=instance,
                        token=drive_token,
                        dir_id=dir_id,
                        filename=pub_filename,
                        content=pdf_content,
                        content_type=pub_mime,
                    )

                twake_drive_link = build_drive_link(instance, dir_id)
                logger.info(
                    "Files uploaded to Twake Drive for %s: %s",
                    recording_id,
                    twake_drive_link,
                )
            else:
                logger.warning(
                    "No room owner with sub found for %s, skipping Twake Drive",
                    recording_id,
                )
        except Exception:
            logger.exception(
                "Twake Drive upload error for %s, continuing", recording_id
            )

    # 6. Send email to the user who triggered the recording (RecordingAccess
    # role=OWNER is created for the trigger user in start_room_recording).
    accesses = await sync_to_async(
        lambda: list(
            recording.accesses.filter(role="owner")
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
                        "room_name": room_name,
                        "recording_date": recording.created_at.astimezone(
                            user.timezone
                        ).strftime("%Y-%m-%d"),
                        "recording_time": recording.created_at.astimezone(
                            user.timezone
                        ).strftime("%H:%M"),
                        "summary_preview": summary_preview,
                        "summary_preview_html": _render_summary_html(summary_preview),
                        "twake_drive_link": twake_drive_link,
                        "has_pdf_attachment": (
                            pdf_content is not None and not twake_drive_link
                        ),
                    }
                    msg_html = render_to_string("mail/html/transcription.html", ctx)
                    msg_plain = render_to_string("mail/text/transcription.txt", ctx)
                    subject = str(_("Your meeting transcription is ready"))
                    email_msg = EmailMultiAlternatives(
                        subject.capitalize(),
                        msg_plain,
                        settings.EMAIL_FROM,
                        [user.email],
                    )
                    email_msg.attach_alternative(msg_html, "text/html")
                    if pdf_content and not twake_drive_link:
                        email_msg.attach(
                            pub_filename,
                            pdf_content,
                            pub_mime,
                        )
                    email_msg.send(fail_silently=False)

            await _send_email()
            logger.info(
                "Sent transcription email to %s for %s",
                user.email,
                recording_id,
            )
        except smtplib.SMTPException:
            logger.warning(
                "Failed to send transcription email to %s",
                user.email,
                exc_info=True,
            )
