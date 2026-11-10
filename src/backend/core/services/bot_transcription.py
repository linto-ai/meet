"""LinTO live transcription of a Meet room, run by the backend.

The backend drives Studio with its own credential, which never reaches the
browser: the room's Studio session, the native bot, the room-wide state and the
end-of-meeting delivery. The native bot republishes the captions into the
LiveKit room; a late joiner reads the transcript so far through Studio's public
session routes.
"""

# pylint: disable=too-many-lines

import json
import time
from datetime import datetime, timezone
from logging import getLogger

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction

import requests

from core import models
from core.api.permissions import get_recording_permission_level
from core.entitlements.capabilities import recording_entitled, summary_entitled
from core.recording.worker.factories import get_worker_service
from core.recording.worker.mediator import WorkerServiceMediator
from core.services.room_management import RoomManagement
from core.services.subtitle import SubtitleService
from core.utils import generate_bot_join_token

logger = getLogger(__name__)

DEFAULT_TIMEOUT = 15
# (connect, read): downloading a whole meeting's audio takes longer.
AUDIO_DOWNLOAD_TIMEOUT = (DEFAULT_TIMEOUT, 300)
# Studio waits for the last captions before it finalizes the conversation.
STOP_TIMEOUT = (DEFAULT_TIMEOUT, 60)
CATCHUP_CHECK_TIMEOUT = 5
# A summary is relayed by a sync gunicorn worker (timeout 90 s): the whole
# stream ends within the deadline plus one read timeout.
CATCHUP_STREAM_DEADLINE = 60
CATCHUP_STREAM_TIMEOUT = (DEFAULT_TIMEOUT, 25)
# One summary per participant every 20 s: the run key serves them all.
CATCHUP_RATE_LIMIT_SECONDS = 20
START_LOCK_TTL = 120
STOP_LOCK_TTL = 90

# ``LINTO_STUDIO_TOKEN_SOURCE`` values — where the backend's Studio credential
# comes from.
TOKEN_SOURCE_SERVICE_ACCOUNT = "service_account"  # noqa: S105
TOKEN_SOURCE_USER_KEY = "user_key"  # noqa: S105


def effective_token_source():
    """Where the backend's Studio credential comes from, once the kill switch
    is applied: ``LINTO_STUDIO_TOKEN_SOURCE``, forced to the service account when
    the per-user feature system is off (``LINTO_ENTITLEMENTS_ENABLED=False``)."""
    if not settings.LINTO_ENTITLEMENTS_ENABLED:
        return TOKEN_SOURCE_SERVICE_ACCOUNT
    return settings.LINTO_STUDIO_TOKEN_SOURCE


# LiveKit room-metadata key lit while a LinTO bot transcribes the room. Read by
# EVERY participant's frontend (banner, CC badge, panel state) — the shared
# source of truth, replayed to late joiners by LiveKit itself.
ROOM_METADATA_STATUS_KEY = "linto_transcription_status"
ROOM_METADATA_STARTER_KEY = "linto_transcription_started_by"

# Studio identity of the RUNNING session, broadcast the same way so a LATE
# JOINER can fetch the transcript so far (catch-up) without ever having seen the
# start: the browser reads these keys, calls studio-api's public session routes
# for the finalized captions and the "before you arrived" summary. They expose
# nothing that ``GET /rooms/{id}/`` does not already return in
# ``configuration["linto"]``.
ROOM_METADATA_SESSION_ID_KEY = "linto_transcription_session_id"
ROOM_METADATA_CHANNEL_ID_KEY = "linto_transcription_channel_id"
ROOM_METADATA_CHANNEL_INDEX_KEY = "linto_transcription_channel_index"
ROOM_METADATA_ORG_ID_KEY = "linto_transcription_org_id"
ROOM_METADATA_STARTED_AT_KEY = "linto_transcription_started_at"
# "1" when the starter left the late-joiner summary on: a participant joining
# mid-run then asks Studio for the "before you arrived" summary; "0" = never.
ROOM_METADATA_CATCHUP_KEY = "linto_transcription_catchup"
# "1" when the run keeps the meeting audio (the live channel's ``keepAudio``):
# every participant then sees the same "recording in progress" indicator as for
# a LiveKit recording, since the audio ends up in Twake Drive; "0" = text only.
ROOM_METADATA_KEEP_AUDIO_KEY = "linto_transcription_keep_audio"

# A Meet room drives ONE session channel, always at index 0 — the index (not
# the channel id) is what namespaces the bot's segment ids
# (``linto:<sessionId>,<channelIndex>:<segmentId>``), so the catch-up history can
# be keyed exactly like the live feed.
LINTO_CHANNEL_INDEX = 0

# Every LinTO room-metadata key, cleared as one block at stop/teardown.
ROOM_METADATA_KEYS = [
    ROOM_METADATA_STATUS_KEY,
    ROOM_METADATA_STARTER_KEY,
    ROOM_METADATA_SESSION_ID_KEY,
    ROOM_METADATA_CHANNEL_ID_KEY,
    ROOM_METADATA_CHANNEL_INDEX_KEY,
    ROOM_METADATA_ORG_ID_KEY,
    ROOM_METADATA_STARTED_AT_KEY,
    ROOM_METADATA_CATCHUP_KEY,
    ROOM_METADATA_KEEP_AUDIO_KEY,
]


def _is_iso_datetime(value):
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def relay_catchup(upstream, deadline=CATCHUP_STREAM_DEADLINE):
    """The bytes of Studio's summary stream, cut after ``deadline`` seconds
    with a final error event. Closing the generator (the participant left)
    closes the upstream request, which makes Studio abort the LLM call."""
    started = time.monotonic()
    try:
        for chunk in upstream.iter_content(chunk_size=None):
            if chunk:
                yield chunk
            if time.monotonic() - started > deadline:
                yield _sse_error("LLM stream timed out")
                return
    except requests.RequestException as exc:
        logger.warning("LinTO catch-up stream interrupted: %s", exc)
        yield _sse_error("LLM stream interrupted")
    finally:
        upstream.close()


def _sse_error(message):
    # Leading blank line: ends a frame Studio may have left half written.
    return f"\n\nevent: error\ndata: {json.dumps({'message': message})}\n\n".encode()


def studio_session_name(room):
    """The room's Studio session; Studio's front hides the ``@…`` names."""
    return f"@meet-{room.id}"


def normalize_profile(profile):
    """A Studio transcriber profile as the panel shows it."""
    config = profile.get("config") or {}
    languages = [
        lang.get("candidate") if isinstance(lang, dict) else lang
        for lang in config.get("languages") or []
    ]
    raw = (
        profile.get("translations")
        or config.get("availableTranslations")
        or config.get("translations")
        or []
    )
    if isinstance(raw, dict):
        translations = [
            code for group in raw.values() if isinstance(group, list) for code in group
        ]
    else:
        translations = list(raw)
    return {
        "id": str(profile.get("_id") or profile.get("id") or ""),
        "name": profile.get("name") or config.get("name") or "",
        "languages": [lang for lang in languages if lang],
        "translations": [code for code in translations if code],
    }


class BotTranscriptionException(Exception):
    """Raised when a transcription-bot operation fails."""


class StudioAudioNotReady(BotTranscriptionException):
    """Studio has no audio file for the conversation yet (HTTP 404).

    A live channel that keeps its audio has it transcoded by the Transcriber
    once the session stops, so the file can trail the conversation for a while.
    """


class PermissionDeniedError(BotTranscriptionException):
    """Raised when the caller may not start/stop the transcription bot.

    The organizer's recording permissions (``transcript_permission`` for the
    bot itself, ``screen_recording_permission`` for the ``record`` add-on) and
    the who-can-stop rule (starter OR room admin/owner) are enforced
    server-side. The viewset answers 403 with ``code='permission_denied'``.
    """


class StudioNotEntitled(BotTranscriptionException):
    """The LinTO option is not active for this participant."""

    def __init__(self, reason="no_entitlement"):
        super().__init__(f"the LinTO option is not active for this account ({reason})")
        self.reason = reason


class RunConflictError(BotTranscriptionException):
    """One live run per room."""

    MESSAGES = {
        "already_running": "a live transcription already runs in this room",
        "already_starting": "a live transcription is already starting in this room",
    }

    def __init__(self, code):
        super().__init__(self.MESSAGES.get(code, code))
        self.code = code


class NoProfileError(BotTranscriptionException):
    """The organization has no live ASR profile to start a run with."""


class TooManyTranslationsError(BotTranscriptionException):
    """More translation languages than LINTO_LIVE_MAX_TRANSLATIONS."""

    def __init__(self, limit):
        super().__init__(f"at most {limit} translation languages")
        self.limit = limit


class CatchUpUnavailable(BotTranscriptionException):
    """No "before you arrived" summary for this room right now."""


class StudioRefusedError(BotTranscriptionException):
    """Studio answered 402 or 403."""

    def __init__(self, what, status, code=None):
        super().__init__(f"Studio refused the {what} ({status} {code or ''})".strip())
        self.status = status
        self.code = code


class BotTranscriptionService:
    """Start/stop a Meet room's Studio live session and its bot."""

    def __init__(self):
        self.studio_base = (settings.LINTO_STUDIO_BASE_URL or "").rstrip("/")
        self._token = None

    # ── auth ─────────────────────────────────────────────────────────────
    def _login(self):
        """The backend's credential towards Studio: the INTEGRATION key when
        configured (identity exchange, admin stop, summary), else a login
        (BOT+MICROPHONE rights via the account's org), else the static
        LINTO_STUDIO_API_TOKEN."""
        if settings.LINTO_STUDIO_INTEGRATION_TOKEN:
            return settings.LINTO_STUDIO_INTEGRATION_TOKEN
        email = settings.LINTO_STUDIO_AUTH_EMAIL
        password = settings.LINTO_STUDIO_AUTH_PASSWORD
        if email and password:
            try:
                r = requests.post(
                    f"{self.studio_base}/auth/login",
                    json={"email": email, "password": password},
                    timeout=DEFAULT_TIMEOUT,
                )
                if r.status_code == 200 and (r.json() or {}).get("auth_token"):
                    return r.json()["auth_token"]
                logger.warning(
                    "Studio service login failed: %s %s", r.status_code, r.text[:200]
                )
            except requests.RequestException as exc:
                logger.warning("Studio service login error: %s", exc)
        if settings.LINTO_STUDIO_API_TOKEN:
            return settings.LINTO_STUDIO_API_TOKEN
        raise BotTranscriptionException(
            "no Studio credentials (set LINTO_STUDIO_AUTH_EMAIL/PASSWORD or "
            "LINTO_STUDIO_API_TOKEN)"
        )

    def _headers(self):
        if not self._token:
            self._token = self._login()
        return {"Authorization": f"Bearer {self._token}"}

    # ── resolution ───────────────────────────────────────────────────────
    def resolve_conversation_id(self, org_id, session_id, name):
        """Find the Studio conversation finalized for a stopped quick session.

        After :meth:`stop` DELETEs the session with a unique ``&name=``,
        Studio's ``storeSessionFromStop`` stores a conversation under that
        name. We look it up by name and confirm it via
        ``type.from_session_id == session_id`` (falling back to the exact name).
        Returns the conversation id, or ``None`` if not found yet / on error
        (the caller retries with a short margin).
        """
        headers = self._headers()
        try:
            r = requests.get(
                f"{self.studio_base}/api/organizations/{org_id}/conversations",
                headers=headers,
                params={"name": name},
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("LinTO conversation resolve error: %s", exc)
            return None
        if r.status_code != 200:
            logger.warning(
                "LinTO conversation resolve returned %s %s",
                r.status_code,
                r.text[:200],
            )
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        items = (
            data
            if isinstance(data, list)
            else (data.get("conversations") or data.get("list") or [])
        )
        # Prefer an exact session match; fall back to an exact-name match.
        for item in items:
            conv_type = item.get("type") or {}
            if (
                isinstance(conv_type, dict)
                and conv_type.get("from_session_id") == session_id
            ):
                return item.get("_id") or item.get("id")
        for item in items:
            if item.get("name") == name:
                return item.get("_id") or item.get("id")
        return None

    # ── conversation audio ───────────────────────────────────────────────
    def _conversation_media(self, method, conversation_id, timeout):
        """``{method} {studio}/api/conversations/{id}/media``: the response when
        Studio has the file, ``None`` when the conversation keeps no audio
        (204). Raises :class:`StudioAudioNotReady` on 404, else
        :class:`BotTranscriptionException` on any failure."""
        try:
            r = requests.request(
                method,
                f"{self.studio_base}/api/conversations/{conversation_id}/media",
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise BotTranscriptionException(
                f"Studio conversation audio error: {exc}"
            ) from exc
        if r.status_code == 204:
            return None
        if r.status_code == 404:
            raise StudioAudioNotReady(
                f"Studio has no audio file for conversation {conversation_id} yet"
            )
        if r.status_code != 200:
            raise BotTranscriptionException(
                f"Studio conversation audio returned {r.status_code}"
            )
        return r

    def conversation_audio_size(self, conversation_id):
        """Size in bytes of the audio Studio keeps for a conversation, read by
        a HEAD request (nothing downloaded): ``None`` when the conversation
        keeps no audio, ``-1`` when Studio does not tell. Raises like
        :meth:`fetch_conversation_audio`."""
        r = self._conversation_media("HEAD", conversation_id, DEFAULT_TIMEOUT)
        if r is None:
            return None
        try:
            return int(r.headers.get("Content-Length", -1))
        except (TypeError, ValueError):
            return -1

    def fetch_conversation_audio(self, conversation_id):
        """The audio Studio archived for a live run, or ``None`` when the
        conversation keeps none (channel started without ``keepAudio``).

        A live channel with a transcriber profile keeps its audio compressed
        (Session-API defaults ``compressAudio`` to true there): an MP3.
        Raises :class:`StudioAudioNotReady` while the Transcriber has not
        written the file yet, :class:`BotTranscriptionException` on any other
        failure.
        """
        r = self._conversation_media("GET", conversation_id, AUDIO_DOWNLOAD_TIMEOUT)
        return None if r is None else r.content

    def _start_recording(self, room, user):
        """Start a LiveKit video egress for the room; never block transcription.

        Returns the recording id on success, else None. The owner is the
        authenticated caller, or the room owner when the caller is anonymous
        (LiveKitTokenAuthentication ⇒ AnonymousUser); if neither exists we still
        record but skip the RecordingAccess.
        """
        try:
            recording = models.Recording.objects.create(
                room=room,
                mode=models.RecordingModeChoices.SCREEN_RECORDING,
                options={},
            )
            owner = user if (user is not None and user.is_authenticated) else None
            if owner is None:
                owner = self._room_owner(room)
            if owner is not None:
                models.RecordingAccess.objects.create(
                    user=owner, role=models.RoleChoices.OWNER, recording=recording
                )
            else:
                logger.warning(
                    "LinTO bot: no owner for egress recording of room %s "
                    "(anonymous trigger, no room owner) — recording without access",
                    room.id,
                )
            worker_service = get_worker_service(mode=recording.mode)
            WorkerServiceMediator(worker_service=worker_service).start(recording)
            return str(recording.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LinTO bot: video egress failed to start for room %s: %s",
                room.id,
                exc,
            )
            return None

    def _stop_recording(self, recording_id):
        """Stop a previously started LiveKit egress; robust to absence."""
        try:
            recording = models.Recording.objects.get(
                id=recording_id, status=models.RecordingStatusChoices.ACTIVE
            )
        except models.Recording.DoesNotExist:
            return
        try:
            worker_service = get_worker_service(mode=recording.mode)
            WorkerServiceMediator(worker_service=worker_service).stop(recording)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LinTO bot: video egress failed to stop (%s): %s", recording_id, exc
            )

    # ── organizer recording permissions + "in progress" banner ───────────
    def _has_recording_permission(self, room, user, mode):
        """Honor the organizer ("Commandes de l'organisateur") recording
        permission for ``mode`` (transcript / screen_recording): an
        ``authenticated`` level lets any logged-in user act, otherwise only a
        room admin/owner; an anonymous participant is denied for admin_owner."""
        level = get_recording_permission_level(mode, room=room)
        if level == "authenticated":
            return bool(user is not None and getattr(user, "is_authenticated", False))
        return room.is_administrator_or_owner(user)

    def _set_banner(self, room, active, user=None, linto=None):
        """Light/clear the room-wide "transcription in progress" state.

        Writes DEDICATED LiveKit room-metadata keys (independent of the egress
        ``recording_mode``/``recording_status`` machinery, so both can coexist).
        Every participant's frontend reads them (banner, CC badge, panel state,
        catch-up, recording indicator) and LiveKit replays them to late
        joiners. When ``linto`` (the run state persisted by :meth:`mark_started`)
        is given, the Studio ids, the start timestamp and whether the run keeps
        the meeting audio ride along, so a LATE JOINER can hydrate the
        transcript so far and sees the recording indicator. Best-effort: a
        metadata failure must never break start/stop.
        """
        # RoomManagement.update_metadata is already @async_to_sync — call it directly.
        try:
            if active:
                # Same id as ``configuration["linto"]["user_id"]`` / bot-status so
                # every participant's frontend can compute "started by me".
                starter = (
                    str(user.id)
                    if (user is not None and getattr(user, "is_authenticated", False))
                    else ""
                )
                metadata = {
                    ROOM_METADATA_STATUS_KEY: "active",
                    ROOM_METADATA_STARTER_KEY: starter,
                }
                run = linto or {}
                # Only advertise ids we actually have: a half-written key set
                # would make the catch-up hook fetch a session that does not exist.
                if run.get("session_id"):
                    metadata[ROOM_METADATA_SESSION_ID_KEY] = str(run["session_id"])
                    metadata[ROOM_METADATA_CHANNEL_INDEX_KEY] = LINTO_CHANNEL_INDEX
                    metadata[ROOM_METADATA_STARTED_AT_KEY] = (
                        datetime.now(timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
                    )
                if run.get("channel_id"):
                    metadata[ROOM_METADATA_CHANNEL_ID_KEY] = str(run["channel_id"])
                if run.get("org_id"):
                    metadata[ROOM_METADATA_ORG_ID_KEY] = str(run["org_id"])
                metadata[ROOM_METADATA_CATCHUP_KEY] = (
                    "1" if run.get("catchup", True) else "0"
                )
                metadata[ROOM_METADATA_KEEP_AUDIO_KEY] = (
                    "1" if run.get("keep_audio") else "0"
                )
                RoomManagement().update_metadata(str(room.id), metadata)
            else:
                RoomManagement().update_metadata(
                    str(room.id),
                    {},
                    list(ROOM_METADATA_KEYS),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LinTO banner metadata update failed for room %s: %s", room.id, exc
            )

    def _stop_native_subtitles(self, room):
        """Tear down the native (upstream) subtitle agent, if any, on takeover.

        Native captions and LinTO captions would otherwise BOTH flow into the
        overlay (double transcription). Best-effort and idempotent.
        """
        if not settings.ROOM_SUBTITLE_ENABLED:
            return
        try:
            SubtitleService().stop_subtitle(room)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LinTO bot: failed to stop the native subtitle agent for room %s: %s",
                room.id,
                exc,
            )

    def _room_owner(self, room):
        """The room's OWNER user, if any (for anonymous-triggered egress ownership)."""
        access = (
            room.accesses.filter(role=models.RoleChoices.OWNER)
            .select_related("user")
            .first()
        )
        return access.user if access else None

    # ── live run ─────────────────────────────────────────────────────────

    def _acting_as(self, user):
        credential = self.studio_token_for(user)
        if not credential.get("enabled") or not credential.get("token"):
            raise StudioNotEntitled(credential.get("reason") or "no_entitlement")
        # `metadata.quickMeeting=false` on a key: no live run under it.
        if (credential.get("capabilities") or {}).get("quickMeeting") is False:
            raise StudioNotEntitled("quick_meeting_disabled")
        organization_id = credential.get("organization_id")
        if not organization_id:
            raise BotTranscriptionException(
                "the Studio credential has no organization to act in"
            )
        return {"Authorization": f"Bearer {credential['token']}"}, organization_id

    def _run_headers(self, linto, user=None):
        # A run started with the starter's own key is stopped with it.
        if effective_token_source() == TOKEN_SOURCE_USER_KEY:
            starter_id = linto.get("user_id")
            starter = None
            if (
                user is not None
                and getattr(user, "is_authenticated", False)
                and str(user.id) == str(starter_id)
            ):
                starter = user
            elif starter_id:
                try:
                    starter = models.User.objects.filter(id=starter_id).first()
                except (ValueError, ValidationError):
                    starter = None
            if starter is not None:
                try:
                    return self._acting_as(starter)[0]
                except BotTranscriptionException as exc:
                    logger.warning(
                        "LinTO: cannot act as the starter %s of the run (%s), "
                        "using the backend credential",
                        starter_id,
                        exc,
                    )
        return self._headers()

    def _studio(self, method, path, headers, what, timeout=DEFAULT_TIMEOUT, **kwargs):
        try:
            r = requests.request(
                method,
                f"{self.studio_base}{path}",
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise BotTranscriptionException(f"Studio {what} error: {exc}") from exc
        if r.status_code in (402, 403):
            try:
                code = (r.json() or {}).get("code")
            except (ValueError, AttributeError):
                code = None
            raise StudioRefusedError(what, r.status_code, code)
        if r.status_code not in (200, 201, 204):
            raise BotTranscriptionException(
                f"Studio {what} returned {r.status_code}: {r.text[:200]}"
            )
        try:
            return r.json() or {}
        except ValueError:
            return {}

    def _studio_delete(self, path, headers, what, timeout=DEFAULT_TIMEOUT, **kwargs):
        try:
            r = requests.delete(
                f"{self.studio_base}{path}",
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            logger.warning("LinTO %s failed: %s", what, exc)
            return
        if r.status_code >= 400 and r.status_code != 404:
            logger.warning("LinTO %s returned %s %s", what, r.status_code, r.text[:200])

    def quick_meeting_profiles(self, user):
        """The live ASR profiles of the organization ``user`` starts a run in."""
        headers, organization_id = self._acting_as(user)
        return self._list_profiles(headers, organization_id)

    def _list_profiles(self, headers, organization_id):
        data = self._studio(
            "GET",
            f"/api/organizations/{organization_id}/transcriber_profiles",
            headers,
            "transcriber profiles",
            params={"quickMeeting": "true"},
        )
        raw = (
            data
            if isinstance(data, list)
            else (data.get("transcriber_profiles") or data.get("list") or [])
        )
        return [normalize_profile(p) for p in raw if isinstance(p, dict)]

    @staticmethod
    def _pick_profile(profiles):
        pinned = str(settings.LINTO_STUDIO_DEFAULT_PROFILE_ID or "")
        ids = [p["id"] for p in profiles if p.get("id")]
        chosen = pinned if pinned in ids else (ids[0] if ids else None)
        if chosen is None:
            raise NoProfileError("no live ASR profile available for this organization")
        # Session-API profile ids are integers.
        return int(chosen) if chosen.isdigit() else chosen

    @staticmethod
    def _session_meta(room, channel_id=None):
        # Studio gives it to the conversation's organization members.
        meta = {"membersRight": settings.LINTO_STUDIO_MEMBERS_RIGHT}
        if settings.LINTO_VISIO_NATIVE_ENABLED:
            descriptor = {
                "livekitUrl": settings.LINTO_NATIVE_LIVEKIT_URL,
                "room": str(room.id),
            }
            if channel_id is not None:
                descriptor["token"] = generate_bot_join_token(
                    str(room.id), str(channel_id)
                )
            meta["native"] = {"visio-native": descriptor}
            # One-release back-compat alias read by the Scheduler.
            meta["linto_native"] = dict(descriptor)
        return meta

    @staticmethod
    def _room_url(room, origin=None):
        base = (settings.EMAIL_APP_BASE_URL or origin or "").rstrip("/")
        return f"{base}/{room.slug}"

    def start(self, room, data, user=None, origin=None):
        """Start the room's live run: Studio session, bot, room-wide state."""
        if not self._has_recording_permission(
            room, user, models.RecordingModeChoices.TRANSCRIPT
        ):
            raise PermissionDeniedError(
                "the organizer restricts transcription to administrators/owners"
            )
        lock = f"linto:start:{room.id}"
        if not cache.add(lock, "1", START_LOCK_TTL):
            raise RunConflictError("already_starting")
        try:
            room.refresh_from_db(fields=["configuration"])
            if (room.configuration or {}).get("linto"):
                raise RunConflictError("already_running")
            return self._start_run(room, data, user, origin)
        finally:
            cache.delete(lock)

    def _start_run(self, room, data, user, origin):
        translations = data.get("translations")
        if not isinstance(translations, list):
            translations = []
        translations = list(
            dict.fromkeys(code for code in translations if isinstance(code, str))
        )
        limit = settings.LINTO_LIVE_MAX_TRANSLATIONS
        if limit and len(translations) > limit:
            raise TooManyTranslationsError(limit)
        headers, organization_id = self._acting_as(user)
        profile_id = self._pick_profile(self._list_profiles(headers, organization_id))
        channel = {
            "name": "Main",
            "transcriberProfileId": profile_id,
            "enableLiveTranscripts": True,
            "diarization": True,
            "keepAudio": bool(settings.LINTO_LIVE_KEEP_AUDIO),
            "translations": translations,
        }
        sessions = f"/api/organizations/{organization_id}/sessions"
        session = self._studio(
            "POST",
            f"{sessions}/",
            headers,
            "session creation",
            json={
                "name": studio_session_name(room),
                # The late joiners read it through Studio's public routes.
                "visibility": "public",
                "channels": [channel],
                "meta": self._session_meta(room),
            },
        )
        session_id = session.get("id") or session.get("_id")
        channel_id = ((session.get("channels") or [{}])[0] or {}).get("id")
        if not session_id or channel_id is None:
            raise BotTranscriptionException(
                "Studio session creation returned no session or channel id"
            )
        bot_id = None
        try:
            # LinTO takes over the captions: stop the native subtitle agent.
            self._stop_native_subtitles(room)
            if settings.LINTO_VISIO_NATIVE_ENABLED:
                self._studio(
                    "PATCH",
                    f"{sessions}/{session_id}",
                    headers,
                    "session meta update",
                    json={"meta": self._session_meta(room, channel_id)},
                )
            bot = self._studio(
                "POST",
                f"/api/organizations/{organization_id}/bots",
                headers,
                "bot launch",
                json={
                    "url": self._room_url(room, origin),
                    "channelId": channel_id,
                    "provider": settings.LINTO_BOT_PROVIDER or "visio",
                    "enableDisplaySub": True,
                    "subSource": "original",
                },
            )
            bot_id = bot.get("id") or bot.get("_id")
            choices = ("summary", "summary_service", "record", "catchup")
            run = self.mark_started(
                room,
                {
                    **{key: data[key] for key in choices if key in data},
                    "keep_audio": bool(settings.LINTO_LIVE_KEEP_AUDIO),
                    "session_id": str(session_id),
                    "channel_id": channel_id,
                    "org_id": organization_id,
                    "bot_id": str(bot_id) if bot_id else None,
                },
                user=user,
            )
        except Exception:
            self._stop_studio_run(headers, organization_id, session_id, bot_id)
            raise
        if run.get("catchup"):
            self._check_catchup(room, headers, organization_id, session_id)
        return run

    @staticmethod
    def _catchup_path(organization_id, session_id):
        return f"/api/organizations/{organization_id}/sessions/{session_id}/catchup"

    def _catchup_status(self, headers, organization_id, session_id):
        """Whether Studio can summarize the session: ``(True, None)`` or
        ``(False, reason)``. Raises requests.RequestException."""
        path = f"{self._catchup_path(organization_id, session_id)}/status"
        r = requests.get(
            f"{self.studio_base}{path}",
            headers=headers,
            timeout=CATCHUP_CHECK_TIMEOUT,
        )
        try:
            body = r.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        if r.status_code == 200 and body.get("enabled"):
            return True, None
        if r.status_code == 200:
            return False, (
                "Studio resolves no LLM Gateway service for the catch-up "
                "(LLM_CATCHUP_SERVICE, default visio-catchup, then "
                "LLM_CHAT_SERVICE_ID), or the SaaS plan of the organization "
                "refuses ai.chat"
            )
        if r.status_code == 404 and not body.get("code"):
            return False, f"Studio has no catch-up routes (GET {path} answered 404)"
        return False, (
            f"GET {path} answered {r.status_code} "
            f"{body.get('code') or body.get('message') or r.text[:200]}"
        )

    def _check_catchup(self, room, headers, organization_id, session_id):
        """Log why the late joiners of the run will get no summary, if so."""
        try:
            enabled, reason = self._catchup_status(headers, organization_id, session_id)
        except requests.RequestException as exc:
            logger.warning(
                "LinTO catch-up status unknown for room %s: %s", room.id, exc
            )
            return
        if not enabled:
            logger.warning(
                "LinTO catch-up unavailable for room %s, late joiners get no summary: %s",
                room.id,
                reason,
            )

    @staticmethod
    def _catchup_run(room):
        linto = (room.configuration or {}).get("linto") or {}
        if not linto:
            raise CatchUpUnavailable("no live transcription runs in the room")
        if not linto.get("catchup"):
            raise CatchUpUnavailable("the run was started without the catch-up")
        if not linto.get("org_id") or not linto.get("session_id"):
            raise CatchUpUnavailable("the run has no Studio session")
        return linto

    def catchup_status(self, room):
        """``{"enabled": True}``, or ``{"enabled": False, "reason": ...}``."""
        try:
            linto = self._catchup_run(room)
            enabled, reason = self._catchup_status(
                self._run_headers(linto), linto["org_id"], linto["session_id"]
            )
        except CatchUpUnavailable as exc:
            return {"enabled": False, "reason": str(exc)}
        except (BotTranscriptionException, requests.RequestException) as exc:
            return {"enabled": False, "reason": f"Studio unreachable: {exc}"}
        return {"enabled": True} if enabled else {"enabled": False, "reason": reason}

    def catchup_stream(self, room, before=None):
        """Ask Studio for the summary of what was said before ``before`` (ISO
        8601). Returns the open streamed response, or None when there is
        nothing to summarize yet. Raises CatchUpUnavailable."""
        linto = self._catchup_run(room)
        path = self._catchup_path(linto["org_id"], linto["session_id"])
        body = {"channelIndex": LINTO_CHANNEL_INDEX}
        if _is_iso_datetime(before):
            body["before"] = before
        try:
            r = requests.post(
                f"{self.studio_base}{path}",
                headers=self._run_headers(linto),
                json=body,
                stream=True,
                timeout=CATCHUP_STREAM_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise CatchUpUnavailable(f"POST {path} failed: {exc}") from exc
        if r.status_code == 204:
            r.close()
            return None
        if r.status_code != 200:
            reason = f"POST {path} answered {r.status_code} {r.text[:200]}"
            r.close()
            raise CatchUpUnavailable(reason)
        return r

    def _stop_studio_run(self, headers, organization_id, session_id, bot_id, name=None):
        if not organization_id:
            return
        if bot_id:
            self._studio_delete(
                f"/api/organizations/{organization_id}/bots/{bot_id}",
                headers,
                "bot stop",
            )
        if session_id:
            params = {"force": "true"}
            if name:
                params["name"] = name
            self._studio_delete(
                f"/api/organizations/{organization_id}/sessions/{session_id}",
                headers,
                "session stop",
                timeout=STOP_TIMEOUT,
                params=params,
            )

    def stop(self, room, user=None, enforce_permission=True):
        """Stop the room's live run: bot, Studio session, room-wide state."""
        lock = f"linto:stop:{room.id}"
        if not cache.add(lock, "1", STOP_LOCK_TTL):
            return {"status": "stopping"}
        try:
            room.refresh_from_db(fields=["configuration"])
            linto = (room.configuration or {}).get("linto")
            if not linto:
                return {"status": "idle"}
            if enforce_permission:
                self._check_may_stop(room, linto, user)
            conversation_name = f"linto-{room.id}-{int(time.time())}"
            try:
                headers = self._run_headers(linto, user)
            except BotTranscriptionException as exc:
                logger.warning("LinTO: no Studio credential to stop the run: %s", exc)
            else:
                self._stop_studio_run(
                    headers,
                    linto.get("org_id"),
                    linto.get("session_id"),
                    linto.get("bot_id"),
                    name=conversation_name,
                )
            return self._close_run(room, linto, conversation_name)
        finally:
            cache.delete(lock)

    @staticmethod
    def _check_may_stop(room, linto, user):
        starter_id = linto.get("user_id")
        is_starter = bool(
            user is not None
            and getattr(user, "is_authenticated", False)
            and starter_id
            and str(user.id) == str(starter_id)
        )
        if not (is_starter or room.is_administrator_or_owner(user)):
            raise PermissionDeniedError(
                "only the starter or a room admin/owner can stop the transcription"
            )

    def _close_run(self, room, linto, conversation_name):
        self._set_banner(room, False)
        recording_id = linto.get("recording_id")
        if recording_id:
            self._stop_recording(recording_id)

        summary = bool(linto.get("summary", True))
        org_id = linto.get("org_id")
        session_id = linto.get("session_id")
        # The delivery (document, Twake Drive, recap email) follows every run
        # with a session; `summary` only says whether the LLM step runs in it.
        summary_payload = None
        if org_id and session_id:
            summary_payload = {
                "org_id": org_id,
                "session_id": session_id,
                "conversation_name": conversation_name,
                "recipient_user_id": linto.get("user_id"),
                "summary": summary,
                "summary_service": linto.get("summary_service"),
                "record": bool(linto.get("record", False)),
                "steps": {},
                "attempts": 0,
            }
        self._write_configuration(
            room,
            {} if summary_payload is None else {"linto_summary": summary_payload},
            drop=("linto",),
        )

        if summary_payload is not None:
            self._enqueue_summary(room)
        logger.info("LinTO run stopped for room %s (summary=%s)", room.id, summary)
        return {"status": "stopped"}

    def mark_started(self, room, data, user=None):
        """Record a started run and light the room-wide state.

        ``data`` carries the Studio ids :meth:`start` obtained (``session_id``,
        ``channel_id``, ``org_id``, ``bot_id``) plus the add-ons
        (``summary`` default True, ``summary_service`` the chosen summary
        service route, ``record`` default False, ``catchup`` default True = a
        late joiner gets the "before you arrived" summary, ``keep_audio`` the
        channel's ``keepAudio``). Re-checks the
        transcript permission (defence in depth), drops both summaries without
        the ``summary`` capability, starts the optional video egress when allowed,
        persists ``room.configuration["linto"]`` and sets the banner. Returns the
        persisted state.
        """
        if not self._has_recording_permission(
            room, user, models.RecordingModeChoices.TRANSCRIPT
        ):
            raise PermissionDeniedError(
                "the organizer restricts transcription to administrators/owners"
            )
        summary = bool(data.get("summary", True))
        catchup = bool(data.get("catchup", True))
        record = bool(data.get("record", False))
        # The run keeps the meeting audio when its channel was created with
        # keepAudio, which follows the instance (LINTO_LIVE_KEEP_AUDIO). It
        # drives the recording indicator every participant sees, so it errs on
        # the side of showing.
        keep_audio = bool(
            settings.LINTO_LIVE_KEEP_AUDIO or data.get("keep_audio") is True
        )
        # Both summaries — at stop, and the late joiners' catch-up — are LLM
        # summaries: neither runs without the `summary` capability.
        if (summary or catchup) and not summary_entitled(user):
            logger.warning(
                "LinTO: summaries requested without the summary capability for "
                "room %s — dropping them",
                room.id,
            )
            summary = catchup = False
        if record and not self._has_recording_permission(
            room, user, models.RecordingModeChoices.SCREEN_RECORDING
        ):
            logger.warning(
                "LinTO: record requested without screen_recording permission for "
                "room %s — dropping it",
                room.id,
            )
            record = False
        if record and not recording_entitled(user):
            logger.warning(
                "LinTO: record requested without the recording capability for "
                "room %s — dropping it",
                room.id,
            )
            record = False

        summary_service = data.get("summary_service") or None
        if summary_service is not None and not isinstance(summary_service, str):
            summary_service = None

        linto = {
            "summary": summary,
            # The summary service (LLM Gateway route) the user picked in the
            # panel; None = the instance default (LINTO_LLM_SERVICE_ROUTE).
            "summary_service": summary_service,
            "record": record,
            # Late-joiner summary: advertised in the room metadata so the
            # joiner's browser knows whether to ask for it.
            "catchup": catchup,
            # Advertised in the room metadata: every participant sees the
            # recording indicator while the run keeps the meeting audio.
            "keep_audio": keep_audio,
            "org_id": data.get("org_id"),
            "session_id": data.get("session_id"),
            "channel_id": data.get("channel_id"),
            "bot_id": data.get("bot_id"),
            "user_id": (
                str(user.id) if (user is not None and user.is_authenticated) else None
            ),
        }
        if record:
            recording_id = self._start_recording(room, user)
            if recording_id:
                linto["recording_id"] = recording_id

        self._write_configuration(room, {"linto": linto})
        self._set_banner(room, True, user=user, linto=linto)
        logger.info(
            "LinTO run started for room %s (session=%s bot=%s summary=%s record=%s)",
            room.id,
            linto.get("session_id"),
            linto.get("bot_id"),
            summary,
            record,
        )
        return {"status": "running", **linto}

    @staticmethod
    def _write_configuration(room, update, drop=()):
        """Update the stored configuration under a row lock: the delivery of
        the previous run writes its ``linto_summary`` at the same time."""
        with transaction.atomic():
            stored = models.Room.objects.select_for_update().get(pk=room.pk)
            config = {
                k: v for k, v in (stored.configuration or {}).items() if k not in drop
            }
            config.update(update)
            stored.configuration = config
            stored.save(update_fields=["configuration"])
        room.configuration = config

    def teardown(self, room):
        """Best-effort cleanup when a room ends with a run still active:
        :meth:`stop` without the permission gate."""
        if not (room.configuration or {}).get("linto"):
            return
        self.stop(room, enforce_permission=False)

    def _enqueue_summary(self, room):
        try:
            from core.tasks.linto import (  # noqa: PLC0415
                process_bot_live_summary,
            )

            process_bot_live_summary.delay(str(room.id))
        except Exception as exc:
            logger.exception(
                "LinTO: failed to enqueue live summary for room %s: %s",
                room.id,
                exc,
            )

    # ── Summary services offered to the panel ─────────────────────────────
    # LLM Gateway usage scope of the services Meet lists (gateway 2.6 `scopes`).
    SUMMARY_SERVICE_SCOPE = "meet"
    SUMMARY_SERVICES_CACHE_KEY = "linto:summary-services"
    SUMMARY_SERVICES_CACHE_TTL = 300

    def summary_services(self):
        """The summary services the panel lets the user choose from.

        The LLM Gateway services carrying the ``meet`` scope, listed through
        Studio (``GET /api/services/{org}/llm?scope=meet``) under the service
        account, in the organization the summaries are produced in — for now
        the instance's organization (``LINTO_STUDIO_DEFAULT_ORG_ID``, the
        Linagora one), not yet the user's. Cached 5 min. Each entry:
        ``{route, name, description, icon, default}``; ``icon`` is the
        gateway's ``metadata.icon`` when the admin set one (None otherwise),
        ``default`` marks ``LINTO_LLM_SERVICE_ROUTE`` (else the first one).
        Returns ``[]`` when Studio cannot answer: the panel then shows no
        choice and the summary falls back to the default service.
        """
        cached = cache.get(self.SUMMARY_SERVICES_CACHE_KEY)
        if cached is not None:
            return cached
        try:
            headers = self._headers()
            org_id = self._service_account_org(headers)
            r = requests.get(
                f"{self.studio_base}/api/services/{org_id}/llm",
                params={"scope": self.SUMMARY_SERVICE_SCOPE},
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        except (requests.RequestException, BotTranscriptionException) as exc:
            logger.warning("LinTO: summary services unavailable: %s", exc)
            return []
        if r.status_code != 200:
            logger.warning(
                "LinTO: summary services listing failed: %s %s",
                r.status_code,
                r.text[:200],
            )
            return []
        data = r.json() or []
        raw = data if isinstance(data, list) else data.get("services", [])
        pinned = settings.LINTO_LLM_SERVICE_ROUTE
        services = []
        for service in raw:
            route = service.get("route") or service.get("name")
            if not route:
                continue
            metadata = service.get("metadata") or {}
            services.append(
                {
                    "route": route,
                    "name": service.get("name") or route,
                    "description": service.get("description") or {},
                    "icon": metadata.get("icon") or None,
                    "default": bool(pinned) and pinned in (route, service.get("name")),
                }
            )
        if services and not any(s["default"] for s in services):
            services[0]["default"] = True
        cache.set(
            self.SUMMARY_SERVICES_CACHE_KEY, services, self.SUMMARY_SERVICES_CACHE_TTL
        )
        return services

    TRANSCRIPTION_LANGUAGES_CACHE_KEY = "linto:transcription-languages"
    TRANSCRIPTION_LANGUAGES_CACHE_TTL = 300

    def transcription_languages(self):
        """The languages the deferred transcription can be asked for.

        The union of the languages the STT services of the gateway advertise
        (``GET /api/services`` through Studio under the service account; each
        service carries ``language`` as a comma-separated list, ``*`` meaning
        automatic detection), so the "transcribe after the meeting" panel
        offers exactly what the offline pipeline can honour (``linto.upload``
        picks the service by language). ``*`` always comes first when any
        service detects the language. Cached 5 min. ``[]`` when Studio
        cannot answer: the panel then offers automatic detection only.
        """
        cached = cache.get(self.TRANSCRIPTION_LANGUAGES_CACHE_KEY)
        if cached is not None:
            return cached
        try:
            headers = self._headers()
            r = requests.get(
                f"{self.studio_base}/api/services",
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        except (requests.RequestException, BotTranscriptionException) as exc:
            logger.warning("LinTO: transcription services unavailable: %s", exc)
            return []
        if r.status_code != 200:
            logger.warning(
                "LinTO: transcription services listing failed: %s %s",
                r.status_code,
                r.text[:200],
            )
            return []
        data = r.json() or []
        raw = data if isinstance(data, list) else data.get("services", [])
        auto = False
        codes = []
        for service in raw:
            if not isinstance(service, dict):
                continue
            scope = service.get("scope") or []
            if scope and "stt" not in scope:
                continue
            # NLP services (punctuation, diarization…) are typed; STT ones are not.
            if (service.get("desc") or {}).get("type"):
                continue
            for raw_code in str(service.get("language") or "*").split(","):
                code = raw_code.strip()
                if not code:
                    continue
                if code == "*":
                    auto = True
                elif code not in codes:
                    codes.append(code)
        languages = (["*"] if auto else []) + sorted(codes)
        cache.set(
            self.TRANSCRIPTION_LANGUAGES_CACHE_KEY,
            languages,
            self.TRANSCRIPTION_LANGUAGES_CACHE_TTL,
        )
        return languages

    # ── Studio token of the backend (identity bridge) ────────────────────
    def studio_token_for(self, user):
        """The Studio JWT + organization the backend acts with for ``user``.

        From ``LINTO_STUDIO_TOKEN_SOURCE``:

        - ``service_account`` — the shared service account, acting in
          ``LINTO_STUDIO_DEFAULT_ORG_ID`` (or its first organization).
        - ``user_key`` — the user's OWN LinTO API key, through the studio-api
          identity exchange (``POST /api/auth/external/token``); the
          organization is the key's.

        With the per-user feature system switched off
        (``LINTO_ENTITLEMENTS_ENABLED=False``) the token always comes from the
        service account: there is no per-user key without an entitlement.

        Returns ``{"enabled": True, "token", "organization_id",
        "capabilities"}``, or ``{"enabled": False, "reason"}`` when the option
        is not active for this user. Raises :class:`BotTranscriptionException`
        on misconfiguration / Studio failure.
        """
        source = effective_token_source()
        if source == TOKEN_SOURCE_SERVICE_ACCOUNT:
            return self._service_account_token()
        if source == TOKEN_SOURCE_USER_KEY:
            return self._user_key_token(user)
        raise BotTranscriptionException(
            f"unknown LINTO_STUDIO_TOKEN_SOURCE {source!r} "
            f"(expected {TOKEN_SOURCE_SERVICE_ACCOUNT!r} or {TOKEN_SOURCE_USER_KEY!r})"
        )

    def _service_account_token(self):
        token = self._login()
        headers = {"Authorization": f"Bearer {token}"}
        return {
            "enabled": True,
            "token": token,
            "organization_id": self._service_account_org(headers),
            "capabilities": {"quickMeeting": True},
        }

    def _service_account_org(self, headers):
        """The organization the service account acts in: the configured pin,
        else the account's first organization (no name heuristic)."""
        if settings.LINTO_STUDIO_DEFAULT_ORG_ID:
            return settings.LINTO_STUDIO_DEFAULT_ORG_ID
        try:
            r = requests.get(
                f"{self.studio_base}/api/organizations/",
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise BotTranscriptionException(
                f"Studio organizations lookup error: {exc}"
            ) from exc
        data = r.json() if r.status_code == 200 else []
        orgs = data if isinstance(data, list) else data.get("organizations", [])
        if not orgs:
            raise BotTranscriptionException(
                "the Studio service account has no organization "
                "(set LINTO_STUDIO_DEFAULT_ORG_ID)"
            )
        return orgs[0].get("_id") or orgs[0].get("id")

    # Exchange answers that mean "not for this user" rather than "broken".
    # `no_entitlement` is what Studio answers since the entitlements API v1
    # (nothing declared for that identity, or every feature off); the two older
    # codes are kept so a Studio still on the previous build behaves the same.
    NOT_ENTITLED_CODES = {
        "no_entitlement",
        "no_linked_key",
        "revoked",
        "domain_inactive",
    }

    def _user_key_token(self, user):
        """Exchange the user's identity for a short token of their OWN key.

        ``POST {studio}/api/auth/external/token {provider, subject, email}``
        with the integration credential: studio-api resolves the entitlement of
        that person (their own record, else their email domain's), finds the
        LinTO API key standing for them — creating one just-in-time when they
        have rights and no key yet — and mints a token that expires within the
        hour. A ``404 no_entitlement`` / ``403 revoked`` is a normal answer —
        the option is not active for this user — not an error. Both
        outcomes are cached per user for ``LINTO_STUDIO_TOKEN_CACHE_TTL``
        seconds (never beyond the token's own life).
        """
        cache_key = self._user_token_cache_key(user)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        subject = user.sub or str(user.id)
        email = (user.email or "").strip().lower()
        payload = {"provider": settings.LINTO_IDENTITY_PROVIDER, "subject": subject}
        if email:
            payload["email"] = email
        try:
            r = requests.post(
                f"{self.studio_base}/api/auth/external/token",
                # Harmless for an INTEGRATION key; required when the credential
                # is a system administrator (dev / transition).
                params={"userScope": "backoffice"},
                json=payload,
                headers=self._headers(),
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise BotTranscriptionException(
                f"Studio identity exchange error: {exc}"
            ) from exc

        ttl = max(1, int(settings.LINTO_STUDIO_TOKEN_CACHE_TTL))
        if r.status_code == 200:
            data = r.json() or {}
            if not data.get("token"):
                raise BotTranscriptionException(
                    "Studio identity exchange returned no token"
                )
            expires_in = data.get("expiresIn")
            result = {
                "enabled": True,
                "token": data["token"],
                "organization_id": data.get("organizationId"),
                "expires_in": expires_in,
                "capabilities": data.get("capabilities") or {"quickMeeting": True},
            }
            if isinstance(expires_in, (int, float)) and expires_in > 0:
                # Keep a margin so a cached token is never handed out expired.
                ttl = max(1, min(ttl, int(expires_in) - 60))
            cache.set(cache_key, result, ttl)
            return result

        code = None
        try:
            code = (r.json() or {}).get("code")
        except ValueError:
            pass
        if r.status_code in (403, 404) and code in self.NOT_ENTITLED_CODES:
            result = {"enabled": False, "reason": code}
            cache.set(cache_key, result, ttl)
            logger.info("LinTO: option not active for user %s (%s)", subject, code)
            return result
        raise BotTranscriptionException(
            f"Studio identity exchange failed: {r.status_code} {r.text[:200]}"
        )

    @staticmethod
    def _user_token_cache_key(user):
        return f"linto:studio-token:{settings.LINTO_IDENTITY_PROVIDER}:{user.id}"

    def forget_user_token(self, user):
        """Drop a user's cached exchange result (e.g. after a revocation)."""
        cache.delete(self._user_token_cache_key(user))
