"""Tests for core.tasks._errors transient/permanent failure classification."""

# pylint: disable=redefined-outer-name

import asyncio

import aiohttp
import pytest
from botocore.exceptions import ClientError as BotoClientError
from botocore.exceptions import EndpointConnectionError

from core.tasks._errors import TransientError, classify_external, is_transient


def _aiohttp_response_error(status):
    return aiohttp.ClientResponseError(request_info=None, history=(), status=status)


def _boto_client_error(code, http_status=None):
    error = {"Error": {"Code": code}}
    if http_status is not None:
        error["ResponseMetadata"] = {"HTTPStatusCode": http_status}
    return BotoClientError(error_response=error, operation_name="GetObject")


class _StatusCodeError(Exception):
    """Stand-in for a requests-style error exposing ``status_code``."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class TestIsTransient:
    def test_timeouts_are_transient(self):
        assert is_transient(asyncio.TimeoutError()) is True
        assert is_transient(TimeoutError()) is True

    def test_aiohttp_5xx_is_transient(self):
        assert is_transient(_aiohttp_response_error(500)) is True
        assert is_transient(_aiohttp_response_error(503)) is True

    def test_aiohttp_4xx_is_permanent(self):
        assert is_transient(_aiohttp_response_error(400)) is False
        assert is_transient(_aiohttp_response_error(404)) is False

    def test_aiohttp_connection_error_is_transient(self):
        # A ClientError without a response (connection reset, disconnect…).
        assert is_transient(aiohttp.ClientConnectionError("boom")) is True

    def test_botocore_core_error_is_transient(self):
        assert is_transient(EndpointConnectionError(endpoint_url="http://s3")) is True

    def test_botocore_5xx_is_transient(self):
        assert is_transient(_boto_client_error("InternalError", 500)) is True
        assert is_transient(_boto_client_error("SlowDown", 503)) is True

    def test_botocore_not_found_and_access_denied_are_permanent(self):
        assert is_transient(_boto_client_error("NoSuchKey")) is False
        assert is_transient(_boto_client_error("404")) is False
        assert is_transient(_boto_client_error("AccessDenied")) is False
        assert is_transient(_boto_client_error("403")) is False

    def test_botocore_unknown_client_error_is_transient(self):
        # No recognizable code and no http status → conservatively retry.
        assert is_transient(_boto_client_error("WeirdError")) is True

    def test_generic_status_code_sniffing(self):
        assert is_transient(_StatusCodeError(503)) is True
        assert is_transient(_StatusCodeError(502)) is True
        assert is_transient(_StatusCodeError(404)) is False
        assert is_transient(_StatusCodeError(400)) is False

    def test_runtime_error_is_permanent(self):
        # Business/config errors ("No ASR service available") never retry.
        assert is_transient(RuntimeError("No ASR service available")) is False

    def test_plain_exception_is_permanent(self):
        assert is_transient(ValueError("bad")) is False
        assert is_transient(KeyError("missing")) is False


class TestClassifyExternal:
    def test_no_error_passes_through(self):
        with classify_external("noop"):
            value = 1 + 1
        assert value == 2

    def test_transient_is_wrapped_as_transient_error(self):
        with pytest.raises(TransientError) as exc_info:
            with classify_external("download from S3"):
                raise aiohttp.ClientConnectionError("connection reset")
        # The original cause is chained for diagnostics.
        assert isinstance(exc_info.value.__cause__, aiohttp.ClientConnectionError)
        assert "download from S3" in str(exc_info.value)

    def test_permanent_propagates_unchanged(self):
        with pytest.raises(RuntimeError, match="No ASR service"):
            with classify_external("upload"):
                raise RuntimeError("No ASR service available")

    def test_existing_transient_error_is_reraised_as_is(self):
        original = TransientError("already classified")
        with pytest.raises(TransientError) as exc_info:
            with classify_external("step"):
                raise original
        # Same instance — not re-wrapped.
        assert exc_info.value is original
