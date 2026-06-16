"""Celery base task with failure notification for the recording pipeline.

``NotificationTask`` is the ``base=`` used by the recording-notification tasks
(LinTO transcription, screen-recording-to-Twake). Its ``on_failure`` hook runs
when Celery has exhausted retries OR when a permanent (non-:class:`TransientError`)
exception propagates. It:

- marks the recording terminally FAILED (``NOTIFICATION_FAILED``) so neither the
  storage-polling fallback nor a webhook re-picks it;
- emails ``EMAIL_SUPPORT_EMAIL`` with maximum diagnostic detail (ids, creator,
  failed step, exception, full traceback, attempt count, conversation id);
- emails the recording creator a sober "we could not finish" message.

When Celery is disabled (the inline fallback in ``_task.py``) this base class is
never instantiated by a real Celery app, so the in-task ``try/except`` in each
task is responsible for invoking the same failure path; see the tasks.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.utils.html import escape

from core import models

logger = logging.getLogger(__name__)

try:  # Celery is optional (settings.CELERY_ENABLED gates real tasks).
    from celery import Task as _CeleryTask
except ImportError:  # pragma: no cover - celery is installed in this backend
    _CeleryTask = object


def _first_arg_recording_id(args, kwargs):
    """Extract the recording_id from the task call signature."""
    if args:
        return args[0]
    return kwargs.get("recording_id")


def _step_summary(linto_state):
    """Return (failed_step, done_steps) derived from the checkpoint state."""
    steps = (linto_state or {}).get("steps", {}) or {}
    done = [name for name, ok in steps.items() if ok]
    # The failed step is the first declared step that is not yet done; if all
    # known steps are done the failure is in a later/unguarded stage.
    failed = next((name for name, ok in steps.items() if not ok), "unknown")
    return failed, done


def _resolve_owner(recording):
    """Best-effort resolution of the recording's OWNER user. Never raises."""
    try:
        owner_access = (
            recording.accesses.filter(role=models.RoleChoices.OWNER)
            .select_related("user")
            .order_by("created_at")
            .first()
        )
        if owner_access and owner_access.user:
            return owner_access.user
    except Exception:  # noqa: BLE001 - diagnostics must not fail
        logger.debug("Could not resolve recording owner")
    return None


def _send_admin_failure_email(  # noqa: PLR0913 - diagnostic email needs the full context
    recording, exc, einfo, attempts, conversation_id, owner=None
):
    """Email EMAIL_SUPPORT_EMAIL with full diagnostics. Never raises."""
    support_email = getattr(settings, "EMAIL_SUPPORT_EMAIL", None)
    if not support_email:
        logger.warning(
            "EMAIL_SUPPORT_EMAIL not configured, skipping admin failure email "
            "for recording %s",
            getattr(recording, "id", "?"),
        )
        return

    try:
        room = getattr(recording, "room", None)
        room_id = getattr(room, "id", None)
        room_name = getattr(room, "name", None) or "Meeting"

        creator_email = (owner.email or "?") if owner else "?"

        failed_step, done_steps = _step_summary(getattr(recording, "linto_state", {}))
        traceback_text = getattr(einfo, "traceback", None) or "<no traceback>"

        lines = [
            "A recording notification pipeline FAILED permanently.",
            "",
            f"Recording id : {getattr(recording, 'id', '?')}",
            f"Room id      : {room_id}",
            f"Room name    : {room_name}",
            f"Mode         : {getattr(recording, 'mode', '?')}",
            f"Status       : {getattr(recording, 'status', '?')}",
            f"Creator email: {creator_email}",
            f"Failed step  : {failed_step}",
            f"Done steps   : {', '.join(done_steps) if done_steps else '(none)'}",
            f"Conversation : {conversation_id or '(none)'}",
            f"Attempts     : {attempts}",
            f"Exception    : {type(exc).__name__}: {exc}",
            f"Timestamp    : {timezone.now().isoformat()}",
            "",
            "Traceback:",
            traceback_text,
        ]
        text_body = "\n".join(lines)
        html_body = (
            "<h2>Recording notification pipeline failed</h2>"
            f"<ul>"
            f"<li><b>Recording id:</b> {escape(getattr(recording, 'id', '?'))}</li>"
            f"<li><b>Room id:</b> {escape(room_id)}</li>"
            f"<li><b>Room name:</b> {escape(room_name)}</li>"
            f"<li><b>Mode:</b> {escape(getattr(recording, 'mode', '?'))}</li>"
            f"<li><b>Status:</b> {escape(getattr(recording, 'status', '?'))}</li>"
            f"<li><b>Creator email:</b> {escape(creator_email)}</li>"
            f"<li><b>Failed step:</b> {escape(failed_step)}</li>"
            f"<li><b>Done steps:</b> {escape(', '.join(done_steps) if done_steps else '(none)')}</li>"
            f"<li><b>Conversation:</b> {escape(conversation_id or '(none)')}</li>"
            f"<li><b>Attempts:</b> {escape(attempts)}</li>"
            f"<li><b>Exception:</b> {escape(type(exc).__name__)}: {escape(exc)}</li>"
            f"<li><b>Timestamp:</b> {escape(timezone.now().isoformat())}</li>"
            f"</ul>"
            f"<pre>{escape(traceback_text)}</pre>"
        )

        msg = EmailMultiAlternatives(
            subject=(
                f"[Meet] Recording pipeline failed for {getattr(recording, 'id', '?')}"
            ),
            body=text_body,
            from_email=settings.EMAIL_FROM,
            to=[support_email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=True)
        logger.info(
            "Sent admin failure email to %s for recording %s",
            support_email,
            getattr(recording, "id", "?"),
        )
    except Exception:
        logger.exception(
            "Failed to send admin failure email for recording %s",
            getattr(recording, "id", "?"),
        )


def _send_creator_failure_email(recording, owner=None):
    """Email the recording creator a sober failure notice. Never raises."""
    try:
        if not (owner and owner.email):
            logger.warning(
                "No creator email for recording %s, skipping creator failure email",
                getattr(recording, "id", "?"),
            )
            return

        user = owner
        room = getattr(recording, "room", None)
        room_name = getattr(room, "name", None) or "your meeting"
        brandname = getattr(settings, "EMAIL_BRAND_NAME", None) or "LinTO"
        support_email = getattr(settings, "EMAIL_SUPPORT_EMAIL", None)

        support_line_txt = (
            f"\n\nIf the problem persists, please contact {support_email}."
            if support_email
            else ""
        )
        support_line_html = (
            f"<p>If the problem persists, please contact "
            f'<a href="mailto:{escape(support_email)}">{escape(support_email)}</a>.</p>'
            if support_email
            else ""
        )

        text_body = (
            f"Hello,\n\n"
            f"We were unable to finish processing the recording of "
            f'"{room_name}". Your transcription or recording could not be '
            f"completed.\n\n"
            f"You can try starting a new recording. We are sorry for the "
            f"inconvenience.{support_line_txt}\n\n"
            f"— The {brandname} team"
        )
        html_body = (
            f"<p>Hello,</p>"
            f"<p>We were unable to finish processing the recording of "
            f"<b>{escape(room_name)}</b>. Your transcription or recording could "
            f"not be completed.</p>"
            f"<p>You can try starting a new recording. We are sorry for the "
            f"inconvenience.</p>"
            f"{support_line_html}"
            f"<p>— The {escape(brandname)} team</p>"
        )

        msg = EmailMultiAlternatives(
            subject="Your meeting recording could not be completed",
            body=text_body,
            from_email=settings.EMAIL_FROM,
            to=[user.email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=True)
        logger.info(
            "Sent creator failure email to %s for recording %s",
            user.email,
            getattr(recording, "id", "?"),
        )
    except Exception:
        logger.exception(
            "Failed to send creator failure email for recording %s",
            getattr(recording, "id", "?"),
        )


def handle_pipeline_failure(recording_id, exc, einfo, attempts=None):
    """Terminal failure handling shared by on_failure and the inline fallback.

    Marks the recording NOTIFICATION_FAILED and sends admin + creator emails.
    Fully exception-safe: it logs but never raises.
    """
    try:
        recording = models.Recording.objects.select_related("room").get(id=recording_id)
    except Exception:
        logger.exception("Could not load recording %s in failure handler", recording_id)
        return

    linto_state = getattr(recording, "linto_state", {}) or {}
    conversation_id = linto_state.get("conversation_id")
    if attempts is None:
        attempts = linto_state.get("attempts")

    # Mark terminally failed so polling/webhook never re-pick it.
    try:
        recording.status = models.RecordingStatusChoices.NOTIFICATION_FAILED
        recording.save(update_fields=["status", "updated_at"])
    except Exception:
        logger.exception(
            "Could not set NOTIFICATION_FAILED status for recording %s",
            recording_id,
        )

    owner = _resolve_owner(recording)
    _send_admin_failure_email(
        recording, exc, einfo, attempts, conversation_id, owner=owner
    )
    _send_creator_failure_email(recording, owner=owner)


class NotificationTask(_CeleryTask):
    """Celery base task that notifies admins + creator on terminal failure."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Run when retries are exhausted or a permanent error propagates."""
        recording_id = _first_arg_recording_id(args, kwargs)
        logger.error(
            "Recording task %s failed terminally for recording %s: %s: %s",
            task_id,
            recording_id,
            type(exc).__name__,
            exc,
        )
        if recording_id is None:
            logger.error("on_failure: no recording_id in task args/kwargs")
            return
        try:
            attempts = getattr(getattr(self, "request", None), "retries", None)
            handle_pipeline_failure(recording_id, exc, einfo, attempts=attempts)
        except Exception:
            logger.exception(
                "on_failure handler itself failed for recording %s",
                recording_id,
            )


__all__ = ("NotificationTask", "handle_pipeline_failure")
