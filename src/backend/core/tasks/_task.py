# ruff: noqa: PLC0415

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class _InlineRequest:
    """Minimal stand-in for celery's ``task.request`` in inline mode."""

    def __init__(self):
        self.retries = 0
        self.id = None


class _InlineTask:
    """Lightweight ``self`` passed to ``bind=True`` tasks when Celery is off.

    It mimics just enough of ``celery.Task`` for our tasks: a ``request`` with a
    ``retries`` counter. There is no broker to re-enqueue against inline, so the
    tasks rely on ``autoretry_for`` (a no-op inline) rather than ``self.retry``;
    a failure goes straight to the shared failure handler.
    """

    def __init__(self):
        self.request = _InlineRequest()


class _SimpleExcInfo:
    """Minimal ``einfo`` carrying a ``.traceback`` for inline failure handling."""

    def __init__(self, exc):
        import traceback

        self.exception = exc
        self.traceback = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )


def task(*d_args, **d_kwargs):
    """
    Decorator compatible with Celery's @app.task, but works without Celery.

    If Celery is available, returns a real Celery task (Celery handles
    ``bind``, ``base``, ``autoretry_for``, ``retry_backoff``, ``max_retries``,
    etc.).

    If not, returns a synchronous fallback exposing ``.delay()``/
    ``.apply_async()``. The fallback honours ``bind=True`` (passing an
    ``_InlineTask`` as the first argument) and, on failure, routes directly to
    ``core.tasks._base.handle_pipeline_failure`` (which is self-protecting and
    extracts the recording_id from the call args) so the failure notification
    still fires in the Celery-disabled dev mode. ``autoretry_for`` is NOT
    honoured inline (there is no broker): a transient error simply reaches the
    failure handler like any other.

    Notes:
        Mostly LLM-generated.
    """

    def _fallback_wrap(func, *, bind=False):
        def _run(args, kwargs):
            call_args = (_InlineTask(), *args) if bind else args
            try:
                return func(*call_args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - route every failure to the handler
                from core.tasks._base import handle_pipeline_failure

                # handle_pipeline_failure extracts the recording_id from args
                # and is documented as never-raising, so a single guard around
                # the import is enough. Swallow so a synchronous caller (the
                # notification service) is not impacted.
                recording_id = args[0] if args else kwargs.get("recording_id")
                handle_pipeline_failure(recording_id, exc, _SimpleExcInfo(exc))
                return None

        def delay(*args, **kwargs):
            return _run(args, kwargs)

        def apply_async(args=None, kwargs=None, **_options):
            return _run(args or (), kwargs or {})

        func.delay = delay
        func.apply_async = apply_async
        return func

    # Handle bare decorator usage: @task
    if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
        func = d_args[0]
        if settings.CELERY_ENABLED:
            from meet.celery_app import app as _celery_app

            return _celery_app.task(func)
        return _fallback_wrap(func)

    # Handle parameterized usage: @task(...), e.g. @task(bind=True, base=...)
    def _decorate(func):
        if settings.CELERY_ENABLED:
            from meet.celery_app import app as _celery_app

            return _celery_app.task(*d_args, **d_kwargs)(func)
        return _fallback_wrap(func, bind=bool(d_kwargs.get("bind", False)))

    return _decorate


__all__ = ("task",)
