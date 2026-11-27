"""LinTO catch-up without live transcription (fork): the late joiners' brief."""

# pylint: disable=no-member,too-many-lines

import asyncio
import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db.models import Q

import aiohttp
import botocore
import requests
from asgiref.sync import async_to_sync
from livekit import api as livekit_api

from core import models, utils
from core.entitlements.capabilities import summary_entitled
from core.recording.worker.exceptions import WorkerResponseError
from core.recording.worker.factories import WorkerServiceConfig
from core.recording.worker.services import BaseEgressService
from core.services.audio_extract import COPY_FORMAT, extract_audio_from_video

logger = logging.getLogger(__name__)

STUDIO_TIMEOUT = 15
UPLOAD_TIMEOUT = 120
CREDENTIAL_CACHE_KEY = "linto_catchup:studio_credential"
CREDENTIAL_CACHE_TTL = 600

STATE_PREFIX = "linto_catchup:state:"
STATE_TTL = 12 * 3600
START_LOCK_TTL = UPLOAD_TIMEOUT + 30
STEP_LOCK_TTL = 60
MAX_STEP_ERRORS = 5
SEND_ETA_S = (2, 30)
TRANSCRIBE_ETA_FLOOR_S = 60
SUMMARY_ETA_S = (5, 60)
# Where the progress bar stands at the end of each step.
STEP_PROGRESS = {"sending": 0.05, "transcribing": 0.8, "summarizing": 0.95}
# After the summary service failed, new jobs wait this long (no audio sent nor
# transcribed for a brief that cannot be written).
SUMMARY_UNAVAILABLE_KEY = "linto_catchup:summary_unavailable"
SUMMARY_RETRY_S = 300

PLAYLIST_NAME = "index.m3u8"
LIVE_PLAYLIST_NAME = "live.m3u8"
PLAYLIST_END = "#EXT-X-ENDLIST"
SEGMENT_PREFIX = "seg"
_SEGMENT_RE = re.compile(r"^seg_(\d+)\.ts$")
READ_WORKERS = 8
SEGMENTS_MAX_AGE = timedelta(days=2)
# An egress runs before its recording is saved ACTIVE.
START_GRACE = timedelta(minutes=2)
SETTLED_PARTS = ("done", "error")

# States in which the browser keeps polling.
RUNNING_STATES = ("sending", "transcribing", "summarizing")


class CatchupError(Exception):
    """A catch-up operation fails (LiveKit, storage or Studio)."""


class NotInRoomError(CatchupError):
    """The caller is not a participant of the room."""


class StudioHTTPError(CatchupError):
    """Studio answered with an error status (not a network failure)."""

    def __init__(self, message: str, status: int, detail: str):
        super().__init__(message)
        self.status = status
        self.detail = detail


# ── Eligibility and naming ──────────────────────────────────────────────────


def is_enabled() -> bool:
    """The feature is on for this deployment."""
    return bool(settings.LINTO_CATCHUP_ENABLED and settings.LINTO_STUDIO_BASE_URL)


def records_segments(mode: str) -> bool:
    """An audio recording is recorded as segments while the catch-up is on."""
    return bool(
        is_enabled()
        and settings.LINTO_STUDIO_ENABLED
        and mode == models.RecordingModeChoices.TRANSCRIPT
    )


def is_segmented(recording: models.Recording) -> bool:
    """The recording's own egress writes the segments (no audio file)."""
    return bool((recording.options or {}).get("segments"))


def conversation_name(recording: models.Recording) -> str:
    """The name of the meeting's transcript in Studio."""
    return f"Reunion_{recording.created_at.strftime('%d-%m-%Y_%H-%M')}"


def is_eligible(recording: models.Recording) -> bool:
    """A transcribed recording whose owner may have it summarized."""
    options = recording.options or {}
    transcribed = (
        recording.mode == models.RecordingModeChoices.TRANSCRIPT
        or options.get("original_mode") == models.RecordingModeChoices.TRANSCRIPT
        or bool(options.get("transcribe"))
    )
    if not transcribed:
        return False
    return summary_entitled(recording_owner(recording))


def recording_owner(recording: models.Recording):
    """The owner of the recording, whose rights the brief is produced under."""
    access = (
        recording.accesses.filter(role=models.RoleChoices.OWNER)
        .select_related("user")
        .first()
    )
    return access.user if access else None


def live_run_active(room) -> bool:
    """A live run of its own that offers the catch-up serves the late joiners
    (``linto/catchup``). Live captions on top of a recording keep nothing, and
    a run whose option is off offers nothing: the recording's brief serves."""
    run = (room.configuration or {}).get("linto")
    return bool(run) and not run.get("captions_only") and run.get("catchup", True)


def recording_prefix(recording_id) -> str:
    """Object prefix of a recording's catch-up segments (no trailing slash)."""
    return f"{settings.LINTO_CATCHUP_PREFIX.strip('/')}/{recording_id}"


def _root_prefix() -> str:
    return f"{settings.LINTO_CATCHUP_PREFIX.strip('/')}/"


def catchup_segment_prefix(egress_info) -> Optional[str]:
    """The catch-up prefix an egress writes to, or None for any other egress."""
    request = egress_info.room_composite
    for output in request.segment_outputs:
        if output.filename_prefix.startswith(_root_prefix()):
            return output.filename_prefix
    return None


def recording_id_of(prefix: str) -> Optional[str]:
    """Recording id from a catch-up segment prefix ``catchup/<id>/seg``."""
    parts = prefix[len(_root_prefix()) :].split("/")
    return parts[0] if parts and parts[0] else None


def is_side_capture(egress_info) -> bool:
    """A side capture next to a video recording, not a recording's own egress."""
    prefix = catchup_segment_prefix(egress_info)
    if not prefix:
        return False
    recording_id = recording_id_of(prefix)
    if not recording_id or not utils.is_uuid(recording_id):
        return True
    recording = models.Recording.objects.filter(id=recording_id).only("options").first()
    return not (recording and is_segmented(recording))


# ── Capture lifecycle (LiveKit egress) ─────────────────────────────────────


def _bucket_args() -> dict:
    return {
        "endpoint": settings.AWS_S3_ENDPOINT_URL,
        "access_key": settings.AWS_S3_ACCESS_KEY_ID,
        "secret": settings.AWS_S3_SECRET_ACCESS_KEY,
        "region": settings.AWS_S3_REGION_NAME,
        "bucket": settings.AWS_STORAGE_BUCKET_NAME,
        "force_path_style": True,
    }


@async_to_sync
async def _list_active_egresses(room_name: Optional[str] = None):
    lkapi = utils.create_livekit_client()
    try:
        kwargs = {"active": True}
        if room_name:
            kwargs["room_name"] = room_name
        response = await lkapi.egress.list_egress(
            livekit_api.ListEgressRequest(**kwargs)
        )
        return list(response.items)
    except livekit_api.TwirpError as e:
        raise CatchupError(f"LiveKit list_egress failed: {e.message}") from e
    finally:
        await lkapi.aclose()


def _segments_request(room_name: str, recording_id, s3):
    prefix = recording_prefix(recording_id)
    output = livekit_api.SegmentedFileOutput(
        filename_prefix=f"{prefix}/{SEGMENT_PREFIX}",
        playlist_name=f"{prefix}/{PLAYLIST_NAME}",
        live_playlist_name=f"{prefix}/{LIVE_PLAYLIST_NAME}",
        segment_duration=settings.LINTO_CATCHUP_SEGMENT_DURATION_S,
        s3=s3,
    )
    return livekit_api.RoomCompositeEgressRequest(
        room_name=room_name, audio_only=True, segment_outputs=[output]
    )


class SegmentsEgressService(BaseEgressService):
    """The egress of an audio recording written as segments."""

    hrid = "audio-segments-composite-livekit-egress"

    def start(self, room_name, recording_id, encoding_options=None):
        """Start writing the room's audio as segments; returns the egress id."""
        request = _segments_request(room_name, recording_id, self._s3)
        response = self._handle_request(request, "start_room_composite_egress")
        if not response.egress_id:
            raise WorkerResponseError("Egress ID not found in the response.")
        return response.egress_id


def segments_worker_service() -> SegmentsEgressService:
    """The egress service of a recording written as segments."""
    return SegmentsEgressService(WorkerServiceConfig.from_settings())


@async_to_sync
async def _start_segments_egress(room_name: str, recording_id):
    lkapi = utils.create_livekit_client()
    try:
        request = _segments_request(
            room_name, recording_id, livekit_api.S3Upload(**_bucket_args())
        )
        return await lkapi.egress.start_room_composite_egress(request)
    except livekit_api.TwirpError as e:
        raise CatchupError(f"LiveKit start egress failed: {e.message}") from e
    finally:
        await lkapi.aclose()


@async_to_sync
async def _stop_egress(egress_id: str):
    lkapi = utils.create_livekit_client()
    try:
        await lkapi.egress.stop_egress(
            livekit_api.StopEgressRequest(egress_id=egress_id)
        )
    except livekit_api.TwirpError as e:
        logger.warning("Catch-up egress %s stop failed: %s", egress_id, e.message)
    finally:
        await lkapi.aclose()


def find_capture(room_name: str, recording_id) -> Optional[object]:
    """The active catch-up egress of a recording, if any."""
    prefix = recording_prefix(recording_id)
    for info in _list_active_egresses(room_name):
        seg_prefix = catchup_segment_prefix(info)
        if seg_prefix and seg_prefix.startswith(f"{prefix}/"):
            return info
    return None


def start_capture(recording: models.Recording) -> Optional[str]:
    """Start the side segmented egress of a recording. Idempotent: webhooks
    can be delivered twice. Returns the egress id, or None when not started."""
    if not is_enabled() or not is_eligible(recording) or is_segmented(recording):
        return None
    if recording.status != models.RecordingStatusChoices.ACTIVE:
        return None
    room_name = str(recording.room_id)
    existing = find_capture(room_name, recording.id)
    if existing is not None:
        return existing.egress_id
    info = _start_segments_egress(room_name, recording.id)
    logger.info(
        "Catch-up capture %s started for recording %s", info.egress_id, recording.id
    )
    return info.egress_id


def stop_captures(room_name: str, recording_id=None) -> int:
    """Stop the catch-up egresses of a room (of one recording if given)."""
    stopped = 0
    for info in _list_active_egresses(room_name):
        seg_prefix = catchup_segment_prefix(info)
        if not seg_prefix:
            continue
        if recording_id and recording_id_of(seg_prefix) != str(recording_id):
            continue
        _stop_egress(info.egress_id)
        stopped += 1
        logger.info("Catch-up capture %s stopped (room %s)", info.egress_id, room_name)
    return stopped


def on_recording_ended(recording: models.Recording):
    """Stop a side capture and forget the brief; the segments wait for the transcription."""
    stop_captures(str(recording.room_id), recording.id)
    discard_state(recording.id)


def reconcile() -> dict:
    """Stop orphan captures, delete unneeded segments and the state of ended recordings."""
    stopped, deleted, discarded = 0, 0, 0
    active_ids = set()
    for info in _list_active_egresses():
        seg_prefix = catchup_segment_prefix(info)
        if not seg_prefix:
            continue
        recording_id = recording_id_of(seg_prefix)
        if _is_active(recording_id):
            active_ids.add(recording_id)
            continue
        _stop_egress(info.egress_id)
        stopped += 1
    for recording_id in list_prefixes():
        if recording_id in active_ids or _segments_needed(recording_id):
            continue
        delete_segments(recording_id)
        deleted += 1
    for recording_id in _state_recording_ids():
        if _is_active(recording_id):
            continue
        discard_state(recording_id)
        discarded += 1
    return {"stopped": stopped, "deleted": deleted, "discarded": discarded}


def _is_active(recording_id) -> bool:
    """Recording, or starting for less than START_GRACE."""
    if not utils.is_uuid(str(recording_id)):
        return False
    started = Q(status=models.RecordingStatusChoices.ACTIVE) | Q(
        status=models.RecordingStatusChoices.INITIATED,
        created_at__gt=_now() - START_GRACE,
    )
    return models.Recording.objects.filter(started, id=recording_id).exists()


def _segments_needed(recording_id) -> bool:
    """A recording running, or waiting for its deferred transcription for a
    while; a failed transcription of segments may not have written its file."""
    if not utils.is_uuid(str(recording_id)):
        return False
    waiting = Q(
        status__in=(
            models.RecordingStatusChoices.ACTIVE,
            models.RecordingStatusChoices.STOPPED,
            models.RecordingStatusChoices.SAVED,
            models.RecordingStatusChoices.NOTIFICATION_SUCCEEDED,
        )
    ) | Q(
        status=models.RecordingStatusChoices.NOTIFICATION_FAILED,
        options__segments=True,
    )
    return models.Recording.objects.filter(
        waiting, id=recording_id, created_at__gt=_now() - SEGMENTS_MAX_AGE
    ).exists()


# ── Segments in Meet's bucket ──────────────────────────────────────────────


def _s3():
    return default_storage.connection.meta.client


def _read_object(key: str) -> Optional[str]:
    try:
        obj = _s3().get_object(Bucket=default_storage.bucket_name, Key=key)
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise CatchupError(f"S3 read failed for {key}") from e
    return obj["Body"].read().decode("utf-8")


def _read_bytes(key: str) -> bytes:
    try:
        obj = _s3().get_object(Bucket=default_storage.bucket_name, Key=key)
    except botocore.exceptions.ClientError as e:
        raise CatchupError(f"S3 read failed for {key}") from e
    return obj["Body"].read()


def read_segments(keys: list) -> bytes:
    """The segments' bytes, concatenated in order: MPEG-TS segments of one
    stream concatenate into one playable stream."""
    if not keys:
        return b""
    with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
        return b"".join(pool.map(_read_bytes, keys))


def studio_audio(segments_audio: bytes) -> bytes:
    """The segments' AAC remuxed into M4A, a third lighter than MPEG-TS; the
    MPEG-TS itself when ffmpeg cannot remux it."""
    try:
        return extract_audio_from_video(
            segments_audio, output_format=COPY_FORMAT, input_suffix=".ts"
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        logger.warning("Catch-up audio sent as MPEG-TS, its remux failed: %s", e)
        return segments_audio


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_playlist(text: str) -> list:
    """Segments of an HLS playlist: name, wall-clock start, duration."""
    segments, start, duration = [], None, None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            start = _parse_time(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                duration = None
        elif line and not line.startswith("#"):
            segments.append(
                {
                    "name": line,
                    "start": start,
                    "duration_ms": round(duration * 1000) if duration else None,
                }
            )
            start, duration = None, None
    return segments


def _list_segment_objects(prefix: str) -> list:
    names, token = [], None
    while True:
        kwargs = {"Bucket": default_storage.bucket_name, "Prefix": f"{prefix}/"}
        if token:
            kwargs["ContinuationToken"] = token
        page = _s3().list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if _SEGMENT_RE.match(name):
                names.append(name)
        if not page.get("IsTruncated"):
            return names
        token = page.get("NextContinuationToken")


def list_segments(recording_id) -> list:
    """Uploaded segments of a recording, in order, with wall-clock starts.

    The main playlist is uploaded only every few segments; the live playlist
    holds the last five. Their union covers every segment; a segment present
    in neither (rare) gets its start from its predecessor."""
    prefix = recording_prefix(recording_id)
    by_name = {}
    for playlist in (PLAYLIST_NAME, LIVE_PLAYLIST_NAME):
        for seg in parse_playlist(_read_object(f"{prefix}/{playlist}") or ""):
            if seg["start"] is not None:
                by_name[seg["name"]] = seg
    for name in _list_segment_objects(prefix):
        by_name.setdefault(name, {"name": name, "start": None, "duration_ms": None})

    def index(seg):
        match = _SEGMENT_RE.match(seg["name"])
        return int(match.group(1)) if match else -1

    ordered = sorted(by_name.values(), key=index)
    default_ms = settings.LINTO_CATCHUP_SEGMENT_DURATION_S * 1000
    result: list = []
    for seg in ordered:
        if seg["start"] is None:
            if not result:
                continue
            last = result[-1]
            seg["start"] = last["start"] + timedelta(milliseconds=last["duration_ms"])
        seg["duration_ms"] = seg["duration_ms"] or default_ms
        seg["key"] = f"{prefix}/{seg['name']}"
        result.append(seg)
    return result


def segments_between(
    recording_id, after: Optional[datetime], up_to: Optional[datetime]
) -> list:
    """The segments whose midpoint is after `after` and before `up_to` (None: all)."""
    result = []
    for seg in list_segments(recording_id):
        midpoint = seg["start"] + timedelta(milliseconds=seg["duration_ms"] / 2)
        if up_to is not None and midpoint >= up_to:
            break
        if after is not None and midpoint <= after:
            continue
        result.append(seg)
    return result


def segments_closed_at(recording_id) -> Optional[datetime]:
    """When the egress closed a recording's playlist, or None while it writes it."""
    key = f"{recording_prefix(recording_id)}/{PLAYLIST_NAME}"
    try:
        obj = _s3().get_object(Bucket=default_storage.bucket_name, Key=key)
    except botocore.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise CatchupError(f"S3 read failed for {key}") from e
    if PLAYLIST_END not in obj["Body"].read().decode("utf-8"):
        return None
    return obj["LastModified"]


def list_prefixes() -> list:
    """Recording ids that have catch-up objects in the bucket."""
    ids, token = [], None
    while True:
        kwargs = {
            "Bucket": default_storage.bucket_name,
            "Prefix": _root_prefix(),
            "Delimiter": "/",
        }
        if token:
            kwargs["ContinuationToken"] = token
        page = _s3().list_objects_v2(**kwargs)
        for common in page.get("CommonPrefixes", []):
            recording_id = recording_id_of(common["Prefix"])
            if recording_id:
                ids.append(recording_id)
        if not page.get("IsTruncated"):
            return ids
        token = page.get("NextContinuationToken")


def delete_segments(recording_id) -> int:
    """Delete every object of a recording's catch-up prefix."""
    prefix = f"{recording_prefix(recording_id)}/"
    deleted, token = 0, None
    while True:
        kwargs = {"Bucket": default_storage.bucket_name, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = _s3().list_objects_v2(**kwargs)
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            _s3().delete_objects(
                Bucket=default_storage.bucket_name, Delete={"Objects": keys}
            )
            deleted += len(keys)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    if deleted:
        logger.info(
            "Catch-up segments deleted for %s (%d objects)", recording_id, deleted
        )
    return deleted


# ── Studio, through its standard API ───────────────────────────────────────


class StudioClient:
    """The Studio calls of the catch-up, with the deferred transcription's credential."""

    def __init__(self):
        self.base = (settings.LINTO_STUDIO_BASE_URL or "").rstrip("/")

    @staticmethod
    def _token(refresh=False) -> str:
        token = None if refresh else cache.get(CREDENTIAL_CACHE_KEY)
        if token:
            return token
        # Local import: bot_transcription imports the recording mediator.
        from core.services.bot_transcription import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            BotTranscriptionException,
            BotTranscriptionService,
        )

        try:
            token = BotTranscriptionService()._login()  # noqa: SLF001  # pylint: disable=protected-access
        except BotTranscriptionException as e:
            raise CatchupError(str(e)) from e
        cache.set(CREDENTIAL_CACHE_KEY, token, CREDENTIAL_CACHE_TTL)
        return token

    def sdk(self, refresh=False):
        """The ``linto`` SDK, with the same credential."""
        from linto import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            LinTO,
        )

        return LinTO(auth_token=self._token(refresh), base_url=self.base)

    def _run(self, call):
        """Run an SDK call, renewing a rejected credential once."""
        for refresh in (False, True):
            try:
                return async_to_sync(call)(self.sdk(refresh))
            except aiohttp.ClientResponseError as e:
                if e.status == 401 and not refresh:
                    continue
                raise StudioHTTPError(
                    f"Studio: HTTP {e.status}", e.status, str(e.message)[:200]
                ) from e
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise CatchupError(f"Studio unreachable: {e}") from e
        raise CatchupError("Studio rejected the credential")

    def _call(
        self, method, path, timeout=STUDIO_TIMEOUT, **kwargs
    ) -> requests.Response:
        response = None
        for refresh in (False, True):
            headers = {"Authorization": f"Bearer {self._token(refresh)}"}
            try:
                response = requests.request(
                    method,
                    f"{self.base}/api{path}",
                    headers=headers,
                    timeout=timeout,
                    **kwargs,
                )
            except requests.RequestException as e:
                raise CatchupError(f"Studio unreachable: {e}") from e
            if response.status_code != 401:
                return response
        return response

    def _ok(self, method, path, **kwargs) -> requests.Response:
        response = self._call(method, path, **kwargs)
        if not 200 <= response.status_code < 300:
            try:
                body = response.json() or {}
            except ValueError:
                body = {}
            detail = str(
                (
                    body.get("error") or body.get("message")
                    if isinstance(body, dict)
                    else ""
                )
                or (response.text or "")[:200]
            ).strip()
            raise StudioHTTPError(
                f"Studio {method} {path.split('?')[0]}: HTTP {response.status_code}"
                + (f" {detail}" if detail else ""),
                response.status_code,
                detail,
            )
        return response

    def create(self, audio: bytes, **options) -> str:
        """Create the conversation from the first minutes, open to appended audio."""

        async def call(linto):
            return await linto.upload(
                file=audio,
                number_of_speaker="0",
                enablePunctuation=True,
                partial=True,
                **options,
            )

        return self._run(call)

    def append(self, conversation_id: str, audio: bytes):
        """Append audio after the parts already sent."""

        async def call(linto):
            return await linto.append(conversation_id, audio)

        self._run(call)

    def complete(self, conversation_id: str):
        """Close the conversation, done once its parts are in."""

        async def call(linto):
            return await linto.complete(conversation_id)

        self._run(call)

    def parts(self, conversation_id: str) -> list:
        """The parts of the conversation, each with its state."""
        body = (
            self._ok(
                "GET", f"/conversations/{conversation_id}", params={"key": "jobs"}
            ).json()
            or {}
        )
        transcription = (body.get("jobs") or {}).get("transcription") or {}
        return transcription.get("parts") or []

    def conversation(self, conversation_id: str) -> dict:
        """The conversation: its transcription job and its turns."""
        return self._ok("GET", f"/conversations/{conversation_id}").json() or {}

    def trigger_summary(self, conversation_id: str, service_route: str):
        """Ask the LLM Gateway service for a new summary of the conversation."""
        self._ok(
            "POST",
            f"/conversations/{conversation_id}/download",
            params={"format": service_route, "regenerate": "true"},
        )

    def summary(self, conversation_id: str, service_route: str):
        """(status, text) of the conversation's summary by `service_route`:
        status is ``running``, ``done`` or ``error``."""
        exports = self._ok(
            "GET", f"/conversations/{conversation_id}/export/list"
        ).json()
        export = next(
            (
                e
                for e in (exports if isinstance(exports, list) else [])
                if e.get("format") == service_route
            ),
            None,
        )
        if export is None:
            return "running", None
        status = export.get("status")
        if status == "error":
            return "error", str(export.get("error") or "").strip() or None
        if status not in ("complete", "done"):
            return "running", None
        job_id = export.get("jobId")
        if not job_id:
            return "done", _summary_text(export)
        content = self._ok(
            "GET", f"/conversations/{conversation_id}/export/{job_id}/content"
        ).json()
        return "done", _summary_text(content)

    def delete(self, conversation_id: str):
        """Delete the conversation and its audio in Studio."""
        response = self._call("DELETE", f"/conversations/{conversation_id}")
        if response.status_code not in (200, 204, 404):
            raise CatchupError(f"Studio delete: HTTP {response.status_code}")


def _summary_text(content) -> str:
    if isinstance(content, dict):
        content = content.get("content") or content.get("output") or ""
    return str(content or "").strip()


def _turns_text(conversation: dict) -> str:
    return "\n".join(
        (turn.get("segment") or "").strip()
        for turn in conversation.get("text") or []
        if (turn.get("segment") or "").strip()
    )


# ── State, one entry per recording in Meet's cache ─────────────────────────


def _state_key(recording_id) -> str:
    return f"{STATE_PREFIX}{recording_id}"


def _lock_key(recording_id, name: str) -> str:
    return f"linto_catchup:lock:{name}:{recording_id}"


def load_state(recording_id) -> dict:
    """The last brief, the running job and the last outcome of a recording."""
    return cache.get(_state_key(recording_id)) or {
        "brief": None,
        "job": None,
        "outcome": None,
    }


def save_state(recording_id, state: dict):
    """Keep the state of a recording for the length of a meeting."""
    cache.set(_state_key(recording_id), state, STATE_TTL)


def discard_state(recording_id):
    """Forget the brief and a running job; the conversation stays."""
    cache.delete(_state_key(recording_id))


def held_conversation(recording: models.Recording) -> dict:
    """The meeting's conversation id and the end of the audio sent to it."""
    return dict((recording.linto_state or {}).get("catchup") or {})


def _hold_conversation(recording: models.Recording, held: dict):
    if recording.linto_state is None:
        recording.linto_state = {}
    recording.linto_state["catchup"] = held
    recording.save(update_fields=["linto_state", "updated_at"])


def _state_recording_ids() -> list:
    iter_keys = getattr(cache, "iter_keys", None)
    if iter_keys is None:
        return []
    return [key[len(STATE_PREFIX) :] for key in iter_keys(f"{STATE_PREFIX}*")]


@contextmanager
def _locked(recording_id, name: str, ttl: int):
    key = _lock_key(recording_id, name)
    acquired = cache.add(key, 1, ttl)
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete(key)


# ── Late joiner: the only moment anything is processed ─────────────────────


@async_to_sync
async def _participant_joined_at_ms(room_name: str, identity: str) -> Optional[int]:
    lkapi = utils.create_livekit_client()
    try:
        participant = await lkapi.room.get_participant(
            livekit_api.RoomParticipantIdentity(room=room_name, identity=identity)
        )
    except livekit_api.TwirpError as e:
        if e.code == "not_found":
            return None
        raise CatchupError(f"LiveKit get_participant failed: {e.message}") from e
    finally:
        await lkapi.aclose()
    return participant.joined_at_ms or (participant.joined_at * 1000) or None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _from_ms(value_ms: int) -> datetime:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _active_recording(room) -> Optional[models.Recording]:
    return (
        models.Recording.objects.filter(
            room=room, status=models.RecordingStatusChoices.ACTIVE
        )
        .order_by("-created_at")
        .first()
    )


def _eta_rtf() -> tuple:
    try:
        low, high = (float(v) for v in settings.LINTO_CATCHUP_ETA_RTF[:2])
    except (TypeError, ValueError):
        low, high = 0.05, 0.3
    return low, max(low, high)


def _step_bounds(step: str, audio_s: float) -> tuple:
    """[min, max] seconds a step takes."""
    if step == "sending":
        return SEND_ETA_S
    if step == "transcribing":
        low_rtf, high_rtf = _eta_rtf()
        return audio_s * low_rtf, max(TRANSCRIBE_ETA_FLOOR_S, audio_s * high_rtf)
    return SUMMARY_ETA_S


def _estimate(job: dict) -> tuple:
    """(progress 0..1, [min, max] seconds left or None once the step runs late)."""
    step, audio_s = job["step"], job.get("audio_s") or 0
    started = _parse_time(job.get("step_started_at") or job["started_at"])
    elapsed = (_now() - started).total_seconds()
    low, high = _step_bounds(step, audio_s)
    steps = list(STEP_PROGRESS)
    index = steps.index(step)
    begin = STEP_PROGRESS[steps[index - 1]] if index else 0.0
    progress = begin + (STEP_PROGRESS[step] - begin) * min(1.0, elapsed / high)
    if elapsed > high:
        return progress, None
    later = [_step_bounds(s, audio_s) for s in steps[index + 1 :]]
    left_low = max(0.0, low - elapsed) + sum(bounds[0] for bounds in later)
    left_high = high - elapsed + sum(bounds[1] for bounds in later)
    return progress, [round(left_low), round(max(left_low, left_high))]


def _view(recording, state: dict, joined_at=None) -> dict:
    """What the browser shows: the state, the progress of a running job, and
    the last brief with the end of the meeting it covers."""
    body = {"recording_id": str(recording.id)}
    if joined_at is not None:
        body["joined_at"] = _iso(joined_at)
    job, brief = state.get("job"), state.get("brief")
    if job:
        body["state"] = job["step"]
        body["progress"], body["eta_s"] = _estimate(job)
        body["overdue"] = body["eta_s"] is None
        body["up_to"] = job["up_to"]
    elif cache.get(_lock_key(recording.id, "start")) is not None:
        body["state"] = "sending"
    elif brief:
        body["state"] = "ready"
    else:
        body["state"] = state.get("outcome") or "none"
    if brief:
        body["brief"] = {"text": brief["text"], "covered_until": brief["covered_until"]}
    return body


async def resolve_speaker_collection_ids(linto, recording_id):
    """The organization's voiceprint collection for speaker identification, or None."""
    if not getattr(settings, "LINTO_SPEAKER_IDENTIFICATION_ENABLED", False):
        return None
    try:
        collection_id = await linto.get_org_voiceprint_collection_id()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "Speaker identification resolution failed for %s, continuing without it",
            recording_id,
        )
        return None
    if collection_id:
        logger.info(
            "Speaker identification enabled for %s (collection=%s)",
            recording_id,
            collection_id,
        )
        return [collection_id]
    logger.warning(
        "Speaker identification enabled but no org voiceprint collection found "
        "for %s; continuing without it",
        recording_id,
    )
    return None


def _conversation_options(recording, client) -> dict:
    """The transcript's settings for an audio recording, a brief's next to a video."""
    language = (recording.options or {}).get("language") or "*"
    if not is_segmented(recording):
        return {
            "name": f"Visio catch-up {recording.id}",
            "language": language,
            "enable_diarization": False,
            "members_right": 0,
        }
    return {
        "name": conversation_name(recording),
        "language": language,
        "enable_diarization": True,
        "members_right": getattr(settings, "LINTO_STUDIO_MEMBERS_RIGHT", 0),
        "speaker_collection_ids": async_to_sync(resolve_speaker_collection_ids)(
            client.sdk(), recording.id
        ),
    }


def _send(recording, client, held: dict, segments: list) -> dict:
    """Send the segments to the meeting's conversation, created by the first sending."""
    audio = studio_audio(read_segments([seg["key"] for seg in segments]))
    if held.get("conversation_id"):
        client.append(held["conversation_id"], audio)
    else:
        held["conversation_id"] = client.create(
            audio, **_conversation_options(recording, client)
        )
    end = segments[-1]["start"] + timedelta(milliseconds=segments[-1]["duration_ms"])
    held["covered_until"] = _iso(end)
    _hold_conversation(recording, held)
    logger.info(
        "Catch-up audio of recording %s: %d segments (%.0f s, %d bytes) sent, "
        "conversation %s",
        recording.id,
        len(segments),
        sum(seg["duration_ms"] for seg in segments) / 1000,
        len(audio),
        held["conversation_id"],
    )
    return held


def _start_job(recording, state: dict, up_to: datetime):
    """Plan the sending of the minutes not sent yet, up to `up_to`: the next poll sends them."""
    recording.refresh_from_db(fields=["linto_state"])
    held = held_conversation(recording)
    segments = segments_between(
        recording.id, _parse_time(held.get("covered_until")), up_to
    )
    if not segments:
        if not state.get("brief"):
            state["outcome"] = "too_short"
        return
    end = segments[-1]["start"] + timedelta(milliseconds=segments[-1]["duration_ms"])
    now = _iso(_now())
    state["job"] = {
        "from": _iso(segments[0]["start"]),
        "up_to": _iso(end),
        "step": "sending",
        "started_at": now,
        "step_started_at": now,
        "audio_s": sum(seg["duration_ms"] for seg in segments) / 1000,
        "errors": 0,
    }
    state["outcome"] = None


def _send_job_audio(recording, state: dict):
    """Send the job's minutes, unless the end of the recording already took them."""
    job = state["job"]
    with _locked(recording.id, "start", START_LOCK_TTL) as acquired:
        if not acquired:
            return
        recording.refresh_from_db(fields=["linto_state", "status"])
        held = held_conversation(recording)
        segments = segments_between(
            recording.id,
            _parse_time(held.get("covered_until")),
            _parse_time(job["up_to"]),
        )
        if segments and (
            held.get("conversation_id")
            or recording.status == models.RecordingStatusChoices.ACTIVE
        ):
            try:
                held = _send(recording, StudioClient(), held, segments)
            except StudioHTTPError as e:
                if e.status >= 500:
                    raise
                logger.warning(
                    "Catch-up audio of %s refused by Studio: %s", recording.id, e
                )
                _end_job(state, "ready" if state.get("brief") else "failed")
                return
    if not held.get("conversation_id"):
        _end_job(state, "ready" if state.get("brief") else "failed")
        return
    job.update(
        conversation_id=held["conversation_id"],
        up_to=held["covered_until"],
        step="transcribing",
        step_started_at=_iso(_now()),
    )


def _end_job(state: dict, outcome: str):
    state["job"] = None
    state["outcome"] = outcome


def summary_unavailable() -> Optional[str]:
    """Why the summary service failed lately, or None."""
    return cache.get(SUMMARY_UNAVAILABLE_KEY)


def _mark_summary_unavailable(recording, reason: str):
    route = settings.LINTO_CATCHUP_SUMMARY_SERVICE
    cache.set(SUMMARY_UNAVAILABLE_KEY, f"{route}: {reason}", SUMMARY_RETRY_S)
    logger.warning(
        "Catch-up brief of %s failed: Studio could not run the LLM Gateway "
        "summary service %r (%s). Is it created on the gateway of %s "
        "(UPGRADE.md)? Late joiners get no brief for the next %d s.",
        recording.id,
        route,
        reason,
        settings.LINTO_STUDIO_BASE_URL,
        SUMMARY_RETRY_S,
    )


def _summarize(recording, state: dict, client, conversation_id: str):
    """Ask for the summary of the whole conversation, unless too little was said."""
    text = _turns_text(client.conversation(conversation_id))
    if len(text) < settings.LINTO_CATCHUP_MIN_CHARS:
        _end_job(state, "ready" if state.get("brief") else "too_short")
        return
    try:
        client.trigger_summary(conversation_id, settings.LINTO_CATCHUP_SUMMARY_SERVICE)
    except StudioHTTPError as e:
        # Asking again on this conversation would not help.
        _mark_summary_unavailable(recording, f"HTTP {e.status} {e.detail}")
        _end_job(state, "ready" if state.get("brief") else "failed")
        return
    job = state["job"]
    job["step"] = "summarizing"
    job["step_started_at"] = _iso(_now())


def _advance(recording, state: dict):
    """One step of the running job: the sending, or a few short Studio calls."""
    job = state["job"]
    if (_now() - _parse_time(job["started_at"])).total_seconds() > (
        settings.LINTO_CATCHUP_JOB_TIMEOUT_S
    ):
        logger.warning("Catch-up job of %s timed out", recording.id)
        _end_job(state, "failed")
        return
    try:
        if job["step"] == "sending":
            _send_job_audio(recording, state)
            return
        client = StudioClient()
        conversation_id = job["conversation_id"]
        if job["step"] == "transcribing":
            parts = client.parts(conversation_id)
            if not parts or any(p.get("state") not in SETTLED_PARTS for p in parts):
                return
            if parts[-1].get("state") == "error":
                logger.warning(
                    "Catch-up transcription of %s failed for its last part",
                    recording.id,
                )
            _summarize(recording, state, client, conversation_id)
        elif job["step"] == "summarizing":
            route = settings.LINTO_CATCHUP_SUMMARY_SERVICE
            status, text = client.summary(conversation_id, route)
            if status == "running":
                return
            if status == "error":
                _mark_summary_unavailable(recording, f"the job failed: {text}")
                _end_job(state, "ready" if state.get("brief") else "failed")
                return
            if not text:
                logger.warning(
                    "Catch-up summary of %s failed: the summary is empty", recording.id
                )
                _end_job(state, "ready" if state.get("brief") else "failed")
                return
            state["brief"] = {
                "text": text,
                "covered_until": job["up_to"],
                "generated_at": _iso(_now()),
            }
            _end_job(state, "ready")
            logger.info("Catch-up brief of %s ready", recording.id)
    except CatchupError as e:
        job["errors"] = job.get("errors", 0) + 1
        logger.warning(
            "Catch-up step of %s failed (%s): %s", recording.id, job["errors"], e
        )
        if job["errors"] >= MAX_STEP_ERRORS:
            _end_job(state, "failed")


def request_brief(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
    room, identity: str, *, client_joined_at_ms=None
) -> dict:
    """A participant asks for the brief of what was said before they joined,
    once per arrival. Returns a dict for the browser."""
    if not is_enabled():
        return {"state": "unavailable", "reason": "disabled"}
    if live_run_active(room):
        return {"state": "live"}
    recording = _active_recording(room)
    if recording is None or not is_eligible(recording):
        return {"state": "none"}

    room_name = str(room.id)
    capture = find_capture(room_name, recording.id)
    if capture is None:
        return {
            "state": "unavailable",
            "reason": "no_capture",
            "recording_id": str(recording.id),
        }
    capture_started = (
        _from_ms(capture.started_at // 1_000_000) if capture.started_at else None
    )

    server_joined_ms = _participant_joined_at_ms(room_name, identity)
    if server_joined_ms is None:
        raise NotInRoomError("participant not in the room")
    joined_ms = server_joined_ms
    if client_joined_at_ms:
        # The client can only ask for less: a reload resets LiveKit's join time.
        joined_ms = min(joined_ms, int(client_joined_at_ms))
    joined_at = _from_ms(joined_ms)

    threshold = timedelta(seconds=settings.LINTO_CATCHUP_LATE_THRESHOLD_S)
    if capture_started is None or joined_at <= capture_started + threshold:
        return {"state": "none", "recording_id": str(recording.id)}

    with _locked(recording.id, "start", START_LOCK_TTL) as acquired:
        if not acquired:
            # Another arrival is sending the audio: poll for its job.
            return _view(recording, load_state(recording.id), joined_at)
        state = load_state(recording.id)
        brief = state.get("brief")
        reuse = timedelta(seconds=settings.LINTO_CATCHUP_REUSE_S)
        covered = _parse_time(brief["covered_until"]) if brief else None
        if not state.get("job") and not (covered and covered >= joined_at - reuse):
            unavailable = summary_unavailable()
            if unavailable:
                logger.info(
                    "Catch-up brief of %s not started, the summary service "
                    "failed lately (%s)",
                    recording.id,
                    unavailable,
                )
                return {
                    "state": "unavailable",
                    "reason": "summary_service",
                    "recording_id": str(recording.id),
                }
            _start_job(recording, state, joined_at)
            save_state(recording.id, state)
    return _view(recording, load_state(recording.id), joined_at)


def request_status(room, recording_id: str) -> dict:
    """The brief of a recording for a participant of the room: advances the
    running job by one step (one poll at a time), then answers its state."""
    recording = models.Recording.objects.filter(id=recording_id, room=room).first()
    if recording is None:
        return {"state": "none"}
    if load_state(recording.id).get("job"):
        with _locked(recording.id, "step", STEP_LOCK_TTL) as acquired:
            if acquired:
                state = load_state(recording.id)
                if state.get("job"):
                    _advance(recording, state)
                    save_state(recording.id, state)
    return _view(recording, load_state(recording.id))


# ── End of the recording: the deferred transcription ───────────────────────


def recording_audio(recording: models.Recording) -> bytes:
    """The whole audio of a segmented recording."""
    return read_segments([seg["key"] for seg in list_segments(recording.id)])


def hand_over(recording: models.Recording) -> Optional[str]:
    """Send the last minutes and close the conversation; returns it, or None to upload."""
    with _locked(recording.id, "start", START_LOCK_TTL) as acquired:
        if not acquired:
            raise CatchupError("a late joiner's audio is being sent")
        held = held_conversation(recording)
        if not held.get("conversation_id"):
            return None
        client = StudioClient()
        try:
            segments = segments_between(
                recording.id, _parse_time(held.get("covered_until")), None
            )
            if segments:
                held = _send(recording, client, held, segments)
            client.complete(held["conversation_id"])
        except StudioHTTPError as e:
            if e.status >= 500:
                raise
            logger.warning(
                "Catch-up conversation %s of %s refused the last audio (%s): "
                "the whole audio is uploaded instead",
                held["conversation_id"],
                recording.id,
                e,
            )
            drop_conversation(recording)
            return None
        logger.info(
            "Catch-up conversation %s of %s finished: it is the transcript",
            held["conversation_id"],
            recording.id,
        )
        return held["conversation_id"]


def drop_conversation(recording: models.Recording):
    """Delete the meeting's conversation and forget it."""
    held = held_conversation(recording)
    if not held.get("conversation_id"):
        return
    try:
        StudioClient().delete(held["conversation_id"])
    except CatchupError as e:
        logger.warning(
            "Catch-up conversation %s not deleted: %s", held["conversation_id"], e
        )
    _hold_conversation(recording, {})
