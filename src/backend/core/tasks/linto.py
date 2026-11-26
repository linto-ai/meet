"""Tasks related to LinTO Studio transcription.

Two Celery tasks live here and share the same delivery pipeline:

- ``process_linto_transcription(recording_id)`` — the recording pipeline: the
  finished ``Recording`` is uploaded to LinTO Studio, transcribed, then
  summarized / exported / shared / pushed to Twake Drive / emailed.
- ``process_bot_live_summary(room_id)`` — the autonomous bot flow: the LinTO
  bot already produced a finalized Studio conversation while transcribing the
  room live; the task resolves it and runs the SAME delivery steps, the
  meeting audio Studio kept for the run standing in for the original media.

The delivery steps are factored as backend-agnostic helpers checkpointed
through :class:`_StepCheckpoint`, so both flows are idempotent / resumable on
a Celery retry.
"""

# The SDK / asyncio / service imports are deferred inside the task bodies on
# purpose (they are only needed when a task runs, and the service imports this
# module lazily — see ``BotTranscriptionService._enqueue_summary``).
# ruff: noqa: PLC0415
# pylint: disable=too-many-lines

import asyncio
import json
import logging
import smtplib
from datetime import datetime
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import get_language, override
from django.utils.translation import gettext_lazy as _

from asgiref.sync import async_to_sync, sync_to_async

from core import models
from core.entitlements.capabilities import summary_entitled
from core.recording.enums import FileExtension
from core.services import linto_catchup
from core.services.audio_extract import extract_audio_from_video
from core.services.recipients import (
    attendees_with_email,
    recording_recipient,
    recording_starter,
    room_owner,
)
from core.services.twake_drive import (
    build_linto_shortcut_filename,
    build_recording_filename,
    build_summary_filename,
    build_transcript_document_filename,
    build_transcript_filename,
)
from core.tasks._base import LiveSummaryTask, NotificationTask
from core.tasks._errors import TransientError, classify_external
from core.tasks._state import (
    ensure_state,
    mark_done,
    save_state,
    step_done,
    step_result,
)
from core.tasks._task import task

logger = logging.getLogger(__name__)


async def _await_media(handle, recording_id) -> dict:
    """Register media-poll handlers and await completion, returning ``result``.

    ``result`` carries ``success`` (bool) and, on success, ``media`` (the media
    object). The engine ``"error"`` event maps to a permanent failure
    (``success=False``); a poll ``"exception"`` event (transient HTTP/network
    failure) is captured and RE-RAISED so the caller's ``classify_external``
    block converts it into a :class:`TransientError` → Celery retry. These two
    failure kinds are kept distinct on purpose.

    The SDK emits ``"error"`` without any detail, so on that event the
    transcription job is read back from Studio and returned as
    ``result["job"]`` (best effort, ``None`` when it cannot be read): it is
    the only place the engine's reason is visible.
    """
    done_event = asyncio.Event()
    result = {}
    captured = {}

    def on_done(media_obj):
        result["media"] = media_obj
        result["success"] = True
        done_event.set()

    def on_error(*args):
        result["success"] = False
        done_event.set()

    def on_exception(exc):
        captured["exc"] = exc
        done_event.set()

    handle.on("done", on_done)
    handle.on("error", on_error)
    handle.on("exception", on_exception)

    def on_update(job):
        job_state = job.get("state") if job else "unknown"
        logger.info("LinTO transcription %s: %s", recording_id, job_state)

    handle.on("update", on_update)

    try:
        await asyncio.wait_for(done_event.wait(), timeout=3600)
    finally:
        # Cancel the background poll task whether we exit normally, via the
        # 3600s timeout, or via the re-raised exception below.
        handle.stop()

    if "exc" in captured:
        # Transient poll failure (HTTP 5xx / network): re-raise so the
        # surrounding classify_external(...) maps it to TransientError → retry.
        raise captured["exc"]

    if result.get("success") is False:
        result["job"] = await _read_transcription_job(handle, recording_id)

    return result


async def _read_transcription_job(handle, recording_id):
    """Return the Studio transcription job of a failed conversation, or None."""
    try:
        status = await handle.api_service.get_media_status(mediaId=handle.media_id)
        return (status or {}).get("jobs", {}).get("transcription")
    except Exception:  # diagnostics must not mask the failure
        logger.warning(
            "Could not read the failed LinTO job of recording %s",
            recording_id,
            exc_info=True,
        )
        return None


def _describe_job(job, limit=1000):
    """Render a Studio job for logs and the admin email, truncated."""
    if not job:
        return "no job detail from Studio"
    try:
        text = json.dumps(job, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(job)
    return text if len(text) <= limit else f"{text[:limit]}…"


_RETRY_KWARGS = {
    "bind": True,
    "base": NotificationTask,
    "autoretry_for": (TransientError,),
    "retry_backoff": 5,
    "retry_backoff_max": 600,
    "max_retries": getattr(settings, "RECORDING_NOTIFICATION_MAX_RETRIES", 4),
    "retry_jitter": True,
}


# ── shared, backend-agnostic delivery helpers ────────────────────────────────
# The tag/move, summary, export, share, Twake and email blocks are reused by
# BOTH the recording-based ``process_linto_transcription`` (checkpoints in
# ``recording.linto_state``) and the bot-live ``process_bot_live_summary``
# (checkpoints in ``room.configuration["linto_summary"]["steps"]``). Each block
# is idempotent through a generic ``_StepCheckpoint`` so neither flow re-runs a
# completed step on a retry.


class _StepCheckpoint:
    """Generic idempotent step-checkpoint over a backing store.

    ``get`` is a sync ``(name) -> value`` returning what the step stored when it
    completed: ``True``, or a result a resumed task still needs (the Twake Drive
    link); falsy when not done. ``mark`` is an async ``(name, value)`` that
    stores it and persists it. This lets the same delivery helpers checkpoint
    against a ``Recording`` OR a ``Room`` config dict.
    """

    def __init__(self, *, get, mark):
        self._get = get
        self._mark = mark

    def done(self, name) -> bool:
        """Return whether the named step has already been checkpointed."""
        return bool(self._get(name))

    def result(self, name):
        """What the named step stored beyond "done", or None."""
        value = self._get(name)
        return None if isinstance(value, bool) else value

    async def mark(self, name, value=True):
        """Record the named step as done (with its result) and persist it."""
        await self._mark(name, value)


def _recording_checkpoint(recording):
    """Checkpoint backed by ``recording.linto_state`` (core.tasks._state)."""
    return _StepCheckpoint(
        get=lambda name: step_result(recording, name),
        mark=lambda name, value: mark_done(recording, name, value),
    )


_PUBLICATION_MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
}


def _publication_mime(pub_format):
    """MIME type of the published document."""
    mime_types = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "odt": "application/vnd.oasis.opendocument.text",
    }
    return mime_types.get(pub_format, "application/octet-stream")


def _has_summary(summary_result):
    """Did the summary step produce a report? Then the published document is
    the summary (export through a template), else the plain transcript."""
    return bool(summary_result.get("success") and summary_result.get("job_id"))


def _document_filename(meeting, pub_format, language, summary_result):
    """The published document's name says what it is: the summary, or the
    transcript when there is no summary."""
    if _has_summary(summary_result):
        return build_summary_filename(meeting, pub_format, language)
    return build_transcript_document_filename(meeting, pub_format, language)


async def _tag_and_move(linto, conversation_id, log_id, checkpoint):
    """Tag the conversation as Visio and move it into the Visio folder (soft-fail).

    Each half is checkpointed separately (``tag`` / ``move``) on success.
    """
    if conversation_id and not checkpoint.done("tag"):
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
                    log_id,
                )
                await checkpoint.mark("tag")
            else:
                logger.warning("No tag id resolved for %s, skipping tag", log_id)
        except Exception:
            logger.exception("Tag conversation error for %s, continuing", log_id)

    if conversation_id and not checkpoint.done("move"):
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
                    log_id,
                )
                await checkpoint.mark("move")
            else:
                logger.warning("No folder id resolved for %s, skipping move", log_id)
        except Exception:
            logger.exception("Move conversation error for %s, continuing", log_id)


def _pick_summary_service(services, requested=None):
    """The LLM service route to summarize with: the one the user picked in the
    panel when Studio still offers it, else ``LINTO_LLM_SERVICE_ROUTE``, else
    the first service."""
    known = {s.get("route") for s in services} | {s.get("name") for s in services}
    if requested and requested in known:
        return requested
    if requested:
        logger.warning(
            "Requested summary service %r is not offered any more, falling back",
            requested,
        )
    return (
        getattr(settings, "LINTO_LLM_SERVICE_ROUTE", None)
        or services[0].get("route")
        or services[0].get("name")
    )


async def _summarize_conversation(
    linto, conversation_id, log_id, checkpoint, service_route=None
):
    """Trigger an LLM summary (soft-fail). Returns the ``summary_result`` dict.

    ``service_route`` is the summary service the user chose (the live panel);
    None = the instance default. The dict carries ``success`` and, on success,
    ``content`` / ``job_id``. It is EMPTY when the feature is disabled, the
    step is already checkpointed, no LLM service is available, or the summary
    failed.
    """
    summary_result = {}
    if not getattr(settings, "LINTO_LLM_SUMMARY_ENABLED", True):
        return summary_result
    if checkpoint.done("summary"):
        return summary_result
    try:
        services = await linto.list_llm_services()
        if services:
            service_route = _pick_summary_service(services, service_route)
            logger.info(
                "Triggering LLM summary for %s (service=%s)", log_id, service_route
            )

            summary_handle = await linto.summarize(conversation_id, service_route)
            summary_done = asyncio.Event()

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
                logger.info("LLM summary %s: %s", log_id, status)

            summary_handle.on("done", on_summary_done)
            summary_handle.on("error", on_summary_error)
            summary_handle.on("update", on_summary_update)

            timeout = getattr(settings, "LINTO_LLM_SUMMARY_TIMEOUT", 600)
            await asyncio.wait_for(summary_done.wait(), timeout=timeout)

            if summary_result.get("success"):
                logger.info("LLM summary done for %s", log_id)
                await checkpoint.mark("summary")
            else:
                logger.warning("LLM summary failed for %s, continuing", log_id)
        else:
            logger.info("No LLM services available, skipping summary for %s", log_id)
    except Exception:
        logger.exception("LLM summary error for %s, continuing", log_id)
    return summary_result


async def _generate_document(
    linto, conversation_id, summary_result, log_id, checkpoint
):
    """Export the publication document (soft-fail).

    Returns ``(document_bytes, pub_format)``. With a summary job: export via a
    publication template (``LINTO_PUBLICATION_FORMAT``). Without one: download
    the raw transcription as DOCX/ODT. ``pub_format`` reflects what was actually
    produced; ``document_bytes`` is ``None`` on failure or when the step is
    already checkpointed.
    """
    pdf_content = None
    pub_format = getattr(settings, "LINTO_PUBLICATION_FORMAT", "pdf")

    if checkpoint.done("document"):
        return pdf_content, pub_format

    if _has_summary(summary_result):
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
                    log_id,
                    job_id,
                    template_id,
                )
                pdf_content = await linto.export_with_template(
                    job_id=job_id,
                    format=pub_format,
                    template_id=template_id,
                    # Studio scopes the export by the conversation.
                    conversation_id=conversation_id,
                )
                logger.info(
                    "%s exported for %s: %d bytes",
                    pub_format.upper(),
                    log_id,
                    len(pdf_content) if pdf_content else 0,
                )
                if pdf_content:
                    await checkpoint.mark("document")
            else:
                logger.warning(
                    "No publication template found for %s, skipping document",
                    log_id,
                )
        except Exception:
            logger.exception("Document generation error for %s, continuing", log_id)

    elif conversation_id:
        # No summary: export transcription directly as DOCX
        try:
            download_format = pub_format if pub_format in ("docx", "odt") else "docx"
            logger.info(
                "Downloading transcription as %s for %s", download_format, log_id
            )
            pdf_content = await linto.download_conversation(
                conversation_id=conversation_id,
                format=download_format,
            )
            pub_format = download_format
            logger.info(
                "%s downloaded for %s: %d bytes",
                pub_format.upper(),
                log_id,
                len(pdf_content) if pdf_content else 0,
            )
            if pdf_content:
                await checkpoint.mark("document")
        except Exception:
            logger.exception("Transcription download error for %s, continuing", log_id)

    return pdf_content, pub_format


async def _share_conversation(
    linto, conversation_id, recipient_user, log_id, checkpoint
):
    """Share the conversation with the recipient user (soft-fail)."""
    if (
        recipient_user
        and recipient_user.email
        and conversation_id
        and not checkpoint.done("share")
    ):
        try:
            await linto.share_conversation(
                conversation_id=conversation_id,
                email=recipient_user.email,
                right=31,  # READ+COMMENT+WRITE+SHARE+DELETE (OWNER=32 is reserved)
                notify=False,  # meet/ sends its own recap email — skip LinTO's
            )
            logger.info(
                "Conversation shared with %s (right=31) for %s",
                recipient_user.email,
                log_id,
            )
            await checkpoint.mark("share")
        except Exception:
            logger.exception("Share conversation error for %s, continuing", log_id)


async def _deliver_to_twake(  # noqa: PLR0913 - delivery needs the full context
    *,
    conversation_id,
    recipient_user,
    transcript_text,
    pdf_content,
    pub_filename,
    pub_mime,
    meeting,
    language,
    extra_files_provider,
    log_id,
    checkpoint,
):
    """Upload transcript/media/shortcut/document to Twake Drive (soft-fail).

    ``meeting`` is what the Drive naming reads (``created_at``, ``room``): the
    Recording, or a stand-in for the live flow; ``language`` is the owner's, the
    folder and file names follow it. ``extra_files_provider`` is an optional
    async callable returning a list of ``(filename, content, content_type)`` for
    the original-media bytes (the recording flow uploads the mp4/ogg; the live
    flow the ogg of the audio Studio kept). Returns the Twake Drive folder link (on
    a resume, the one a previous attempt checkpointed, so the email still links
    the files), or ``None`` when Twake is not configured or the upload failed.
    """
    twake_configured = getattr(settings, "CLOUDERY_URL", None) and getattr(
        settings, "CLOUDERY_TOKEN", None
    )
    if not twake_configured:
        return None
    if checkpoint.done("twake"):
        return checkpoint.result("twake")

    twake_drive_link = None
    try:
        from core.services.twake_drive import (
            build_drive_link,
            ensure_meeting_directory,
            get_drive_token,
            resolve_instance,
            save_file,
        )

        instance = resolve_instance(recipient_user) if recipient_user else None
        if instance:
            drive_token = await get_drive_token(
                cloudery_url=settings.CLOUDERY_URL,
                cloudery_token=settings.CLOUDERY_TOKEN,
                instance=instance,
            )

            dir_id = await ensure_meeting_directory(
                instance, drive_token, meeting, language=language
            )

            await save_file(
                instance=instance,
                token=drive_token,
                dir_id=dir_id,
                filename=build_transcript_filename(meeting, language),
                content=transcript_text or "",
                content_type="text/vnd.cozy.note+markdown",
            )

            # Original media bytes (the recording, or the live run's audio).
            if extra_files_provider is not None:
                for filename, content, content_type in await extra_files_provider():
                    await save_file(
                        instance=instance,
                        token=drive_token,
                        dir_id=dir_id,
                        filename=filename,
                        content=content,
                        content_type=content_type,
                    )

            # Upload LinTO Studio shortcut
            frontend_url = getattr(settings, "LINTO_STUDIO_FRONTEND_URL", None)
            if frontend_url and conversation_id:
                shortcut_url = (
                    f"{frontend_url}/interface/conversations/"
                    f"{conversation_id}/transcription"
                )
                shortcut_content = f"[InternetShortcut]\nURL={shortcut_url}\n"
                await save_file(
                    instance=instance,
                    token=drive_token,
                    dir_id=dir_id,
                    filename=build_linto_shortcut_filename(language),
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
                "Files uploaded to Twake Drive for %s: %s", log_id, twake_drive_link
            )
            await checkpoint.mark("twake", twake_drive_link)
        else:
            logger.warning(
                "No Twake instance for the owner of %s, skipping Twake Drive", log_id
            )
    except Exception:
        logger.exception("Twake Drive upload error for %s, continuing", log_id)
    return twake_drive_link


def _mail_context(user, room_name, meeting_dt, **extra):
    """The context shared by the delivery emails, in the user's time zone."""
    local_dt = meeting_dt.astimezone(user.timezone)
    return {
        "brandname": settings.EMAIL_BRAND_NAME,
        "support_email": settings.EMAIL_SUPPORT_EMAIL,
        "logo_img": settings.EMAIL_LOGO_IMG,
        "room_name": room_name,
        "recording_date": local_dt.strftime("%Y-%m-%d"),
        "recording_time": local_dt.strftime("%H:%M"),
        **extra,
    }


@sync_to_async
def _send_mail(user, template, subject, context, attachment=None):
    """Render ``template`` in the user's language and send it to them."""
    with override(user.language or get_language()):
        msg_html = render_to_string(f"mail/html/{template}.html", context)
        msg_plain = render_to_string(f"mail/text/{template}.txt", context)
        email_msg = EmailMultiAlternatives(
            str(subject).capitalize(),
            msg_plain,
            settings.EMAIL_FROM,
            [user.email],
        )
        email_msg.attach_alternative(msg_html, "text/html")
        if attachment:
            email_msg.attach(*attachment)
        email_msg.send(fail_silently=False)


async def _send_recap_emails(  # noqa: PLR0913 - recap email needs the full context
    *,
    recipient_users,
    room_name,
    meeting_dt,
    with_summary,
    twake_drive_link,
    pdf_content,
    pub_filename,
    pub_mime,
    log_id,
    checkpoint,
):
    """Send the recap email, the document attached, to each recipient.

    ``with_summary`` says whether a summary was produced: without one, the
    email announces the transcript alone. The ``email`` step is checkpointed
    only when EVERY send succeeded, so a retry re-sends to all recipients after
    any SMTP failure.
    """
    if checkpoint.done("email"):
        logger.info("Email already sent for %s, skipping", log_id)
        return

    attachment = (pub_filename, pdf_content, pub_mime) if pdf_content else None
    email_failures = False
    for user in recipient_users:
        if not user.email:
            continue
        context = _mail_context(
            user,
            room_name,
            meeting_dt,
            with_summary=with_summary,
            twake_drive_link=twake_drive_link,
            has_pdf_attachment=attachment is not None,
        )
        try:
            await _send_mail(
                user,
                "transcription",
                _("Your meeting transcription is ready"),
                context,
                attachment,
            )
            logger.info("Sent transcription email to %s for %s", user.email, log_id)
        except smtplib.SMTPException:
            email_failures = True
            logger.warning(
                "Failed to send transcription email to %s",
                user.email,
                exc_info=True,
            )

    if not email_failures:
        await checkpoint.mark("email")


async def _share_summary(  # noqa: PLR0913 - the shared summary needs the full context
    *,
    attendee_ids,
    recipient_user,
    room_name,
    meeting_dt,
    summary_result,
    pdf_content,
    pub_filename,
    pub_mime,
    log_id,
    checkpoint,
):
    """Email the summary, the document attached, to the signed-in attendees
    but the recipient. Each one is checkpointed in ``shared`` so a retry skips
    them."""
    if not attendee_ids or not _has_summary(summary_result):
        return
    attendees = await sync_to_async(attendees_with_email)(
        attendee_ids, exclude=recipient_user
    )
    sent = list(checkpoint.result("shared") or [])
    attachment = (pub_filename, pdf_content, pub_mime) if pdf_content else None
    for user in attendees:
        if str(user.id) in sent:
            continue
        context = _mail_context(
            user,
            room_name,
            meeting_dt,
            has_attachment=attachment is not None,
        )
        try:
            await _send_mail(
                user,
                "summary_shared",
                _("A meeting summary has been shared with you"),
                context,
                attachment,
            )
        except smtplib.SMTPException:
            logger.warning(
                "Failed to share the summary with %s for %s",
                user.email,
                log_id,
                exc_info=True,
            )
            continue
        sent.append(str(user.id))
        await checkpoint.mark("shared", list(sent))
        logger.info("Shared the summary with %s for %s", user.email, log_id)


# ── recording pipeline ───────────────────────────────────────────────────────


@task(**_RETRY_KWARGS)
def process_linto_transcription(self, recording_id):
    """Upload recording to LinTO Studio and notify user when done.

    Downloads the audio file from S3 storage, uploads it to LinTO Studio
    via the SDK, waits for transcription completion, optionally triggers
    an LLM summary, generates the document via publication templates, then
    emails it to the room owner and to the attendees it is shared with.

    The pipeline is idempotent: every expensive/external step is checkpointed
    in ``recording.linto_state`` and skipped on a retry if already done. Only
    transient errors (network/timeout/HTTP 5xx) are wrapped as
    :class:`TransientError` and retried by Celery; permanent errors propagate
    to ``NotificationTask.on_failure`` (admin + creator email, status set to
    NOTIFICATION_FAILED).
    """
    _process_linto_transcription_sync(recording_id)


def _catchup_failure_is_transient(step):
    """A catch-up failure that is not an HTTP answer of Studio is retried."""

    def wrap(func):
        def call(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except linto_catchup.StudioHTTPError:
                raise
            except linto_catchup.CatchupError as e:
                raise TransientError(f"Transient failure during '{step}': {e}") from e

        return call

    return wrap


@sync_to_async
def _store_segmented_file(recording):
    """Write a segmented recording's audio file at its usual key."""
    audio = _catchup_failure_is_transient("read the recording's segments")(
        linto_catchup.recording_audio
    )(recording)
    ogg_audio = extract_audio_from_video(audio, output_format="ogg", input_suffix=".ts")
    default_storage.connection.meta.client.put_object(
        Bucket=default_storage.bucket_name,
        Key=recording.key,
        Body=ogg_audio,
        ContentType="audio/ogg",
    )
    return len(audio), len(ogg_audio)


async def _take_catchup_conversation(recording):
    """The late joiners' conversation as the transcript, or None."""
    if not linto_catchup.held_conversation(recording):
        return None
    if not linto_catchup.is_segmented(recording):
        await sync_to_async(linto_catchup.drop_conversation)(recording)
        return None
    with classify_external("hand the meeting's audio over to LinTO Studio"):
        return await sync_to_async(
            _catchup_failure_is_transient("hand the meeting's audio over")(
                linto_catchup.hand_over
            )
        )(recording)


async def _take_catchup_audio(recording, state):
    """Write the segmented recording's file, then hand the conversation over."""
    if linto_catchup.is_segmented(recording) and not step_done(recording, "audio_file"):
        with classify_external("write the recording's audio file"):
            sizes = await _store_segmented_file(recording)
        logger.info(
            "Audio file of %s written from its segments (%d -> %d bytes)",
            recording.id,
            *sizes,
        )
        await mark_done(recording, "audio_file")
    if not state.get("conversation_id"):
        conversation_id = await _take_catchup_conversation(recording)
        if conversation_id:
            state["conversation_id"] = conversation_id
            await save_state(recording)


@sync_to_async
def _release_segments(recording):
    if not (linto_catchup.is_enabled() or linto_catchup.is_segmented(recording)):
        return
    try:
        linto_catchup.delete_segments(recording.id)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Catch-up segments of %s not deleted", recording.id)


async def _summary_wanted(options, starter, recording_id):
    """Does the deferred pipeline ask for an LLM summary? Not when the panel
    turned it off, nor when the user who started the recording does not hold
    the ``summary`` capability — read when the summary is produced, so a right
    withdrawn during the meeting applies."""
    if options.get("summary") is False:
        logger.info("Summary turned off for %s, skipping", recording_id)
        return False
    if not await sync_to_async(summary_entitled)(starter):
        logger.info(
            "No summary capability for the starter of %s, skipping", recording_id
        )
        return False
    return True


@async_to_sync
async def _process_linto_transcription_sync(recording_id):  # noqa: PLR0915
    """Async implementation using LinTO SDK (idempotent / resumable)."""
    from linto import LinTO

    recording = await sync_to_async(
        models.Recording.objects.select_related("room").get
    )(id=recording_id)

    state = ensure_state(recording)
    state["attempts"] = int(state.get("attempts", 0)) + 1
    await save_state(recording)

    await _take_catchup_audio(recording, state)

    # The recording bytes (fetched from the S3 recording storage — any
    # S3-compatible backend reached via the storage client, not just MinIO) are
    # needed (a) to UPLOAD to LinTO Studio when no conversation is checkpointed
    # yet, and (b) to push the original recording to Twake Drive while that step
    # is still pending. On a resume where both are already done we skip the S3
    # GET (and the video→audio extraction).
    conversation_id = state.get("conversation_id")
    twake_configured = getattr(settings, "CLOUDERY_URL", None) and getattr(
        settings, "CLOUDERY_TOKEN", None
    )
    need_upload = not conversation_id
    need_twake_bytes = bool(twake_configured) and not step_done(recording, "twake")

    @sync_to_async
    def download_from_storage():
        s3_client = default_storage.connection.meta.client
        s3_response = s3_client.get_object(
            Bucket=default_storage.bucket_name,
            Key=recording.key,
        )
        return s3_response["Body"].read()

    file_content = None
    audio_content = None
    if need_upload or need_twake_bytes:
        # 1. Download audio from S3 (transient on botocore/network errors)
        logger.info("Downloading %s from S3 storage", recording.key)
        with classify_external("download recording from S3"):
            file_content = await download_from_storage()
        logger.info(
            "Downloaded %d bytes for recording %s",
            len(file_content),
            recording_id,
        )

    # 1.5 Extract audio if source is a video (LinTO only accepts audio) — only
    # needed for the upload. Stream-copy the original AAC track into an .m4a
    # container — no re-encoding, lossless, fast; LinTO resamples internally.
    if need_upload:
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

    # 2. Initialize SDK (auto-discovers org, auto-selects ASR service). Same
    # credential chain as the live flow (integration token → service-account
    # login → static LINTO_STUDIO_API_TOKEN): a deployment whose static token
    # belongs to another Studio (dev-meet's prod token against staging) still
    # transcribes under the service account, in ITS organization.
    from core.services.bot_transcription import (
        BotTranscriptionException,
        BotTranscriptionService,
    )

    try:
        sdk_token = await sync_to_async(BotTranscriptionService()._login)()  # noqa: SLF001  # pylint: disable=protected-access
    except BotTranscriptionException:
        sdk_token = settings.LINTO_STUDIO_API_TOKEN
    linto = LinTO(auth_token=sdk_token, base_url=settings.LINTO_STUDIO_BASE_URL)

    language = recording.options.get("language") or "*"
    conversation_name = linto_catchup.conversation_name(recording)

    # 3. Upload + start transcription — ONLY if not already uploaded. The
    # conversation_id is checkpointed immediately, so a later crash never
    # re-uploads (which would duplicate the Studio conversation).
    if not conversation_id:
        logger.info(
            "Uploading recording %s to LinTO Studio (lang=%s)",
            recording_id,
            language,
        )
        speaker_collection_ids = await linto_catchup.resolve_speaker_collection_ids(
            linto, recording_id
        )

        # RuntimeError (no ASR service / no organizations) is permanent and
        # must propagate without being wrapped → no retry, straight to
        # on_failure. Only network/HTTP-5xx failures become TransientError.
        with classify_external("upload to LinTO Studio"):
            conversation_id = await linto.upload(
                file=audio_content,
                enable_diarization=True,
                number_of_speaker="0",
                language=language,
                enablePunctuation=True,
                name=conversation_name,
                members_right=getattr(settings, "LINTO_STUDIO_MEMBERS_RIGHT", 0),
                speaker_collection_ids=speaker_collection_ids,
            )
        state["conversation_id"] = conversation_id
        await save_state(recording)
        logger.info(
            "Uploaded recording %s, conversation_id=%s",
            recording_id,
            conversation_id,
        )
    else:
        logger.info(
            "Resuming recording %s with existing conversation_id=%s",
            recording_id,
            conversation_id,
        )

    # 4. Wait for transcription. Resume polling on the existing conversation
    # without re-uploading. Skip entirely if already completed.
    media = None
    if not step_done(recording, "transcription"):
        with classify_external("resume LinTO transcription polling"):
            handle = await linto.poll_media(conversation_id)
            result = await _await_media(handle, recording_id)

        if not result.get("success"):
            # Transcription engine reported an error: this is a terminal,
            # non-retryable outcome for this conversation.
            job = _describe_job(result.get("job"))
            logger.error(
                "LinTO Studio transcription failed for recording %s "
                "(conversation %s): %s",
                recording_id,
                conversation_id,
                job,
            )
            raise RuntimeError(
                f"LinTO Studio transcription failed for recording {recording_id} "
                f"(conversation {conversation_id}): {job}"
            )

        media = result["media"]
        await mark_done(recording, "transcription")
        logger.info(
            "Transcription done for %s: %d turns, %d chars",
            recording_id,
            len(media.turns),
            len(media.full_text),
        )
    else:
        # Already transcribed on a prior attempt: re-fetch the media so the
        # downstream steps (document/twake/email) have the transcript text.
        # One-shot fetch (no polling): raises on HTTP failure → classify_external
        # → TransientError retry, so there is never a silent empty transcript.
        logger.info(
            "Transcription already done for %s, re-fetching media", recording_id
        )
        with classify_external("re-fetch LinTO media"):
            # Requires the new linto SDK (get_media one-shot fetch).
            media = await linto.get_media(conversation_id)

    # 5.x Delivery — shared with process_bot_live_summary, checkpointed in
    # recording.linto_state (same steps/semantics as before the extraction).
    checkpoint = _recording_checkpoint(recording)

    # 5.1 / 5.2 Tag + move (soft-fail)
    await _tag_and_move(linto, conversation_id, recording_id, checkpoint)

    # The summary follows the capability of the user who started the
    # recording; the delivery goes to the room owner.
    starter, recipient_user = await sync_to_async(
        lambda: (recording_starter(recording), recording_recipient(recording))
    )()

    # 5.5 LLM summary (soft-fail) — when wanted; the service is the one
    # picked in the panel (None = the instance default).
    options = recording.options or {}
    if await _summary_wanted(options, starter, recording_id):
        summary_result = await _summarize_conversation(
            linto,
            conversation_id,
            recording_id,
            checkpoint,
            service_route=options.get("summary_service") or None,
        )
    else:
        summary_result = {}

    # 5.6 Document attachment (soft-fail)
    pdf_content, pub_format = await _generate_document(
        linto, conversation_id, summary_result, recording_id, checkpoint
    )

    # Prepare publication filename and mime type + summary preview text
    room_name = recording.room.name or "Meeting"
    pub_mime = _publication_mime(pub_format)

    # 5.8 Share the conversation with the room owner. Folder and file names on
    # Twake Drive follow their language.
    owner_language = recipient_user.language if recipient_user else None
    pub_filename = _document_filename(
        recording, pub_format, owner_language, summary_result
    )

    await _share_conversation(
        linto, conversation_id, recipient_user, recording_id, checkpoint
    )

    # 5.7 Upload to Twake Drive via Cloudery (soft-fail), including the
    # original recording bytes (video + extracted audio, or audio only).
    async def _recording_media_files():
        files = []
        if recording.extension == FileExtension.MP4.value:
            files.append(
                (
                    build_recording_filename(recording, "mp4", owner_language),
                    file_content,
                    "video/mp4",
                )
            )
            # Also upload extracted audio alongside the video
            # (OGG for drive — compressed, smaller; WAV was only for LinTO)
            ogg_audio = await sync_to_async(extract_audio_from_video)(
                file_content, output_format="ogg"
            )
            files.append(
                (
                    build_recording_filename(recording, "ogg", owner_language),
                    ogg_audio,
                    "audio/ogg",
                )
            )
        else:
            files.append(
                (
                    build_recording_filename(recording, "ogg", owner_language),
                    file_content,
                    "audio/ogg",
                )
            )
        return files

    transcript_text = (media.full_text or "") if media else ""
    twake_drive_link = await _deliver_to_twake(
        conversation_id=conversation_id,
        recipient_user=recipient_user,
        transcript_text=transcript_text,
        pdf_content=pdf_content,
        pub_filename=pub_filename,
        pub_mime=pub_mime,
        meeting=recording,
        language=owner_language,
        extra_files_provider=_recording_media_files,
        log_id=recording_id,
        checkpoint=checkpoint,
    )

    # 6. Email the room owner, then the attendees the summary is shared with.
    await _send_recap_emails(
        recipient_users=[recipient_user] if recipient_user else [],
        room_name=room_name,
        meeting_dt=recording.created_at,
        with_summary=_has_summary(summary_result),
        twake_drive_link=twake_drive_link,
        pdf_content=pdf_content,
        pub_filename=pub_filename,
        pub_mime=pub_mime,
        log_id=recording_id,
        checkpoint=checkpoint,
    )

    await _share_summary(
        attendee_ids=state.get("attendees") if options.get("share_summary") else None,
        recipient_user=recipient_user,
        room_name=room_name,
        meeting_dt=recording.created_at,
        summary_result=summary_result,
        pdf_content=pdf_content,
        pub_filename=pub_filename,
        pub_mime=pub_mime,
        log_id=recording_id,
        checkpoint=checkpoint,
    )

    await _release_segments(recording)


# ── autonomous bot live-summary (no Recording) ───────────────────────────────
# How many times to re-query Studio for the just-finalized conversation before
# giving up THIS attempt (which raises TransientError → a later Celery retry).
_CONV_RESOLVE_ATTEMPTS = 6
_CONV_RESOLVE_DELAY = 2  # seconds between in-attempt re-queries
# The Transcriber writes a live channel's kept audio once the session stops
# (PCM transcoded to MP3 in place), so Studio may answer 404, or a file still
# growing, for a while after the conversation exists. The file is downloaded
# once two HEADs this far apart report the same size; past the attempts the
# delivery goes on without it.
_AUDIO_READY_ATTEMPTS = 36
_AUDIO_READY_DELAY = 5  # seconds between two size checks


def _store_linto_summary(room, payload):
    """Write (or drop, with None) this delivery's ``linto_summary`` over the
    stored configuration: a run started meanwhile keeps its ``linto``, and a
    newer run's marker is left alone."""
    session_id = ((room.configuration or {}).get("linto_summary") or {}).get(
        "session_id"
    )
    with transaction.atomic():
        stored = models.Room.objects.select_for_update().filter(pk=room.pk).first()
        if stored is None:
            return
        config = dict(stored.configuration or {})
        if (config.get("linto_summary") or {}).get("session_id") != session_id:
            return
        if payload is None:
            config.pop("linto_summary", None)
        else:
            config["linto_summary"] = payload
        stored.configuration = config
        stored.save(update_fields=["configuration"])


@sync_to_async
def _save_room_config(room):
    """Persist the pending ``linto_summary`` marker."""
    _store_linto_summary(room, (room.configuration or {}).get("linto_summary"))


@sync_to_async
def _clear_linto_summary(room):
    """Drop the pending ``linto_summary`` marker once delivery is complete."""
    _store_linto_summary(room, None)
    (room.configuration or {}).pop("linto_summary", None)


def _room_summary_checkpoint(room):
    """Checkpoint backed by ``room.configuration["linto_summary"]["steps"]``."""

    def _get(name):
        payload = (room.configuration or {}).get("linto_summary") or {}
        return (payload.get("steps") or {}).get(name)

    async def _mark(name, value):
        room.configuration["linto_summary"].setdefault("steps", {})[name] = value
        await _save_room_config(room)

    return _StepCheckpoint(get=_get, mark=_mark)


async def _resolve_summary_recipient(payload, room):
    """Recipient of the live delivery: the room owner, else the starter.

    Returns ``None`` when neither exists — the caller degrades gracefully.
    """
    owner = await sync_to_async(room_owner)(room)
    if owner is not None:
        return owner
    user_id = payload.get("starter_user_id")
    if user_id:
        try:
            return await sync_to_async(models.User.objects.get)(id=user_id)
        except (models.User.DoesNotExist, ValueError, ValidationError):
            logger.warning(
                "Starter %s not found for room %s, no recipient", user_id, room.id
            )
    return None


async def _resolve_live_conversation_id(service, payload, room_id):
    """Find the Studio conversation finalized by the session DELETE.

    Re-queries a few times with a short delay (the conversation is stored by
    Studio's ``storeSessionFromStop`` before the DELETE returns, but allow
    some margin). Still not found → :class:`TransientError` so Celery
    retries the whole task later with back-off.
    """
    org_id = payload["org_id"]
    session_id = payload["session_id"]
    conversation_name = payload["conversation_name"]
    for attempt in range(_CONV_RESOLVE_ATTEMPTS):
        with classify_external("resolve LinTO conversation id"):
            conversation_id = await sync_to_async(service.resolve_conversation_id)(
                org_id, session_id, conversation_name
            )
        if conversation_id:
            return conversation_id
        if attempt < _CONV_RESOLVE_ATTEMPTS - 1:
            await asyncio.sleep(_CONV_RESOLVE_DELAY)
    raise TransientError(
        f"LinTO conversation for room {room_id} not found yet "
        f"(session {session_id}, name {conversation_name})"
    )


async def _summarize_live_run(linto, conversation_id, room_id, checkpoint, payload):
    """The live run's summary when its payload asks for one (the starter left
    it on and held the capability, checked at start), else ``{}``: the
    delivery then carries the transcript alone. A payload written before
    ``summary`` was carried only existed with the summary on."""
    if not payload.get("summary", True):
        logger.info("Summary off for room %s, delivering the transcript", room_id)
        return {}
    return await _summarize_conversation(
        linto,
        conversation_id,
        room_id,
        checkpoint,
        service_route=payload.get("summary_service"),
    )


async def _fetch_live_audio(service, conversation_id, log_id):
    """The audio Studio kept for a live run once complete, else ``None``.

    Soft by design: no audio (a run started without ``keepAudio``), a file
    that never shows up or any Studio failure is logged and answers ``None``,
    so the transcript, the document and the email still go out.
    """
    from core.services.bot_transcription import (
        BotTranscriptionException,
        StudioAudioNotReady,
    )

    last_size = None
    for attempt in range(_AUDIO_READY_ATTEMPTS):
        try:
            size = await sync_to_async(service.conversation_audio_size)(conversation_id)
        except StudioAudioNotReady:
            size = 0
        except BotTranscriptionException as exc:
            logger.warning("Live audio size check failed for %s: %s", log_id, exc)
            size = 0
        if size is None:
            logger.info("The live run of %s kept no audio", log_id)
            return None
        # -1: Studio does not tell the size, nothing to wait on.
        if size == -1 or (size > 0 and size == last_size):
            try:
                return await sync_to_async(service.fetch_conversation_audio)(
                    conversation_id
                )
            except BotTranscriptionException as exc:
                logger.warning("Live audio download failed for %s: %s", log_id, exc)
                return None
        last_size = size
        if attempt < _AUDIO_READY_ATTEMPTS - 1:
            await asyncio.sleep(_AUDIO_READY_DELAY)
    logger.warning(
        "Live audio of %s not ready after %d checks, delivering without it",
        log_id,
        _AUDIO_READY_ATTEMPTS,
    )
    return None


def _live_audio_files(service, conversation_id, meeting, language, log_id):
    """``extra_files_provider`` of the live delivery: the meeting audio as an
    OGG next to the transcript, named like the recording flow's, or nothing
    when it is unavailable (never fails the Twake Drive step)."""

    async def _provide():
        audio = await _fetch_live_audio(service, conversation_id, log_id)
        if not audio:
            return []
        try:
            ogg_audio = await sync_to_async(extract_audio_from_video)(
                audio, output_format="ogg", input_suffix=".mp3"
            )
        except Exception:  # the audio never blocks the delivery
            logger.exception("Live audio conversion failed for %s", log_id)
            return []
        return [
            (
                build_recording_filename(meeting, "ogg", language),
                ogg_audio,
                "audio/ogg",
            )
        ]

    return _provide


@task(**{**_RETRY_KWARGS, "base": LiveSummaryTask})
def process_bot_live_summary(self, room_id):
    """Summarize + deliver a LinTO bot's OWN live-transcription conversation.

    Enqueued by ``BotTranscriptionService.stop`` for every run with a
    session; the LLM summary step runs only when the payload's ``summary`` is
    true (the starter's choice, gated by the ``summary`` capability), the
    document, Twake Drive and recap email always go out. It is INDEPENDENT of
    the recording pipeline / ``Recording`` model:
    it resolves the Studio conversation that the session DELETE finalized,
    then reuses the shared tag/summary/export/share/Twake/email delivery helpers;
    the meeting audio Studio kept for the run (``keepAudio`` channel) goes to
    Twake Drive as an OGG when it is available, never blocking the rest.

    Progress is checkpointed in ``room.configuration["linto_summary"]["steps"]``
    so a retry resumes; only transient failures retry (conversation not stored
    yet, Studio HTTP 5xx / network), and the task degrades gracefully when there
    is no recipient with an email (share/Twake/email skipped, logged). The
    ``linto_summary`` marker is removed once delivery completes.
    """
    _process_bot_live_summary_sync(room_id)


@async_to_sync
async def _process_bot_live_summary_sync(room_id):
    """Async implementation of the autonomous bot live-summary delivery."""
    from linto import LinTO

    from core.services.bot_transcription import BotTranscriptionService

    try:
        room = await sync_to_async(models.Room.objects.get)(id=room_id)
    except models.Room.DoesNotExist:
        # The room was deleted (meeting ended + cleanup) before the summary ran —
        # nothing to summarize/deliver. No-op (do NOT retry).
        logger.info("Room %s gone before LinTO summary ran, skipping", room_id)
        return
    payload = (room.configuration or {}).get("linto_summary")
    if not payload:
        logger.info("No pending LinTO summary for room %s, skipping", room_id)
        return

    if not all(payload.get(k) for k in ("org_id", "session_id", "conversation_name")):
        logger.warning(
            "Incomplete linto_summary payload for room %s: %s — aborting",
            room_id,
            payload,
        )
        await _clear_linto_summary(room)
        return

    payload["attempts"] = int(payload.get("attempts", 0)) + 1
    # Pin the meeting timestamp on the first attempt so retries keep the same
    # Twake folder / document names.
    payload.setdefault("stopped_at", timezone.now().isoformat())
    await _save_room_config(room)

    checkpoint = _room_summary_checkpoint(room)
    service = BotTranscriptionService()

    # 1. Resolve the finalized Studio conversation (checkpointed so a retry
    # never re-queries Studio by name).
    conversation_id = payload.get("conversation_id")
    if not conversation_id:
        conversation_id = await _resolve_live_conversation_id(service, payload, room_id)
        payload["conversation_id"] = conversation_id
        await _save_room_config(room)
        logger.info(
            "Resolved LinTO conversation %s for room %s live summary",
            conversation_id,
            room_id,
        )

    # 2. Initialize the SDK and fetch the finalized media (TEXT + per-stream
    # speaker names). Transient on HTTP failure. Use the SAME auth as the bot
    # (service login → fresh auth_token) rather than the static
    # LINTO_STUDIO_API_TOKEN, so the task works with login-only deployments.
    sdk_token = await sync_to_async(service._login)()  # noqa: SLF001  # pylint: disable=protected-access
    linto = LinTO(auth_token=sdk_token, base_url=settings.LINTO_STUDIO_BASE_URL)
    with classify_external("fetch LinTO media"):
        media = await linto.get_media(conversation_id)

    # 3. Resolve the recipient (graceful when none with email).
    recipient_user = await _resolve_summary_recipient(payload, room)
    if recipient_user is None or not recipient_user.email:
        logger.warning(
            "No recipient with email for room %s live summary — "
            "share/Twake/email will be skipped",
            room_id,
        )

    # 4. Tag + move + summarize + document (recipient-independent).
    await _tag_and_move(linto, conversation_id, room_id, checkpoint)
    summary_result = await _summarize_live_run(
        linto, conversation_id, room_id, checkpoint, payload
    )
    pdf_content, pub_format = await _generate_document(
        linto, conversation_id, summary_result, room_id, checkpoint
    )

    room_name = room.name or "Meeting"
    meeting_dt = datetime.fromisoformat(payload["stopped_at"])
    # What the Twake Drive naming needs from a "recording": the live flow has
    # none, so the meeting stands in (date/time of the stop, room).
    meeting = SimpleNamespace(created_at=meeting_dt, room=room)
    language = recipient_user.language if recipient_user else None
    pub_mime = _publication_mime(pub_format)
    pub_filename = _document_filename(meeting, pub_format, language, summary_result)

    # 5. Share + Twake + email. Twake Drive also gets the meeting audio Studio
    # kept for the run, as an OGG (soft: the text files go without it).
    await _share_conversation(
        linto, conversation_id, recipient_user, room_id, checkpoint
    )

    transcript_text = (media.full_text or "") if media else ""
    twake_drive_link = await _deliver_to_twake(
        conversation_id=conversation_id,
        recipient_user=recipient_user,
        transcript_text=transcript_text,
        pdf_content=pdf_content,
        pub_filename=pub_filename,
        pub_mime=pub_mime,
        meeting=meeting,
        language=language,
        extra_files_provider=_live_audio_files(
            service, conversation_id, meeting, language, room_id
        ),
        log_id=room_id,
        checkpoint=checkpoint,
    )

    recipient_users = (
        [recipient_user] if (recipient_user and recipient_user.email) else []
    )
    await _send_recap_emails(
        recipient_users=recipient_users,
        room_name=room_name,
        meeting_dt=meeting_dt,
        with_summary=_has_summary(summary_result),
        twake_drive_link=twake_drive_link,
        pdf_content=pdf_content,
        pub_filename=pub_filename,
        pub_mime=pub_mime,
        log_id=room_id,
        checkpoint=checkpoint,
    )

    await _share_summary(
        attendee_ids=payload.get("attendees") if payload.get("share_summary") else None,
        recipient_user=recipient_user,
        room_name=room_name,
        meeting_dt=meeting_dt,
        summary_result=summary_result,
        pdf_content=pdf_content,
        pub_filename=pub_filename,
        pub_mime=pub_mime,
        log_id=room_id,
        checkpoint=checkpoint,
    )

    # Delivery complete — drop the pending marker so a later start/stop is clean.
    await _clear_linto_summary(room)
    logger.info(
        "LinTO live summary completed for room %s (conversation %s)",
        room_id,
        conversation_id,
    )
