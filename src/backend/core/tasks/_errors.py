"""Error classification helpers for the recording notification pipeline.

These utilities split failures of the external services (the S3 recording
storage, the LinTO
Studio SDK, Twake Drive / Cloudery) into two buckets:

- **Transient** — network blips, timeouts and HTTP 5xx. These are worth a
  Celery retry with exponential back-off, so they are re-raised as
  :class:`TransientError` (the only exception class wired into
  ``autoretry_for`` on the tasks).
- **Permanent** — HTTP 4xx, configuration/business errors (``RuntimeError``
  such as "No ASR service available" or "User has no organizations") and any
  programming error. These are propagated **as-is** so Celery does NOT retry
  them: they go straight to ``Task.on_failure``.

Usage::

    from core.tasks._errors import classify_external, TransientError

    with classify_external("download recording from S3"):
        file_content = await download_from_storage()

Anything raised inside the ``with`` block that looks transient is converted to
``TransientError`` (chained from the original); everything else is left
untouched so it propagates unchanged.
"""

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)


class TransientError(Exception):
    """A retryable failure of an external dependency.

    Only this class is registered in the tasks' ``autoretry_for`` tuple, so a
    failure must be wrapped in it to be retried by Celery; everything else is
    considered permanent and reaches ``on_failure`` immediately.
    """


def _http_status(exc):
    """Best-effort extraction of an HTTP status code from an exception."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def is_transient(exc) -> bool:
    """Return True when ``exc`` represents a retryable (transient) failure.

    Transient = network/timeout errors, HTTP 5xx responses, and non-404
    botocore errors. HTTP 4xx and everything else (notably ``RuntimeError``
    configuration/business errors) are treated as permanent.
    """
    # Timeouts (asyncio + the stdlib alias) are always transient.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True

    # aiohttp client errors (connection issues, server disconnects, etc.).
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - aiohttp is a hard dep here
        aiohttp = None

    if aiohttp is not None:
        if isinstance(exc, aiohttp.ClientResponseError):
            # HTTP error with a status: 5xx is transient, 4xx is permanent.
            return exc.status >= 500
        if isinstance(exc, aiohttp.ClientError):
            # Connection/timeout/disconnect errors without a response.
            return True

    # botocore / S3 errors.
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # pragma: no cover - botocore is a hard dep here
        BotoCoreError = ClientError = ()

    if BotoCoreError and isinstance(exc, BotoCoreError):
        # Connection/endpoint/credential plumbing errors — retry.
        return True
    if ClientError and isinstance(exc, ClientError):
        # A response came back: 404/NoSuchKey is permanent, 5xx is transient.
        error = getattr(exc, "response", {}).get("Error", {}) or {}
        code = str(error.get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound", "403", "AccessDenied"}:
            return False
        http_status = (
            getattr(exc, "response", {})
            .get("ResponseMetadata", {})
            .get("HTTPStatusCode")
        )
        if isinstance(http_status, int):
            return http_status >= 500
        # Unknown S3 client error: be conservative and retry.
        return True

    # Generic HTTP status sniffing (e.g. requests.HTTPError) as a last resort.
    status = _http_status(exc)
    if status is not None:
        return status >= 500

    return False


@contextlib.contextmanager
def classify_external(step):
    """Re-raise transient failures of an external call as ``TransientError``.

    ``step`` is a short human label used in the log line. Permanent errors are
    re-raised unchanged so Celery does not retry them.
    """
    try:
        yield
    except TransientError:
        # Already classified upstream — let it bubble for retry.
        raise
    except Exception as exc:
        if is_transient(exc):
            logger.warning(
                "Transient failure during '%s': %s: %s (will retry)",
                step,
                type(exc).__name__,
                exc,
            )
            raise TransientError(f"Transient failure during '{step}': {exc}") from exc
        # Permanent: propagate as-is so it reaches on_failure without retry.
        raise


__all__ = ("TransientError", "classify_external", "is_transient")
