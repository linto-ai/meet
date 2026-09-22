"""LinTO live-transcription lifecycle helpers for a Meet room (browser-first).

The BROWSER owns the Studio control plane via the LinTO JS SDK, authenticated as
the real user: it creates the quickMeeting, chooses the ASR profile/translations
and starts/stops the bot. This service only does what the browser cannot:

- ``prepare`` — gate on the organizer's ``transcript_permission`` and MINT the
  native LiveKit bot join token (needs Meet's LiveKit secret) + stop the native
  subtitle agent on takeover;
- ``mark_started`` / ``mark_stopped`` — drive the room-wide banner, the optional
  video egress and the autonomous post-meeting summary, and persist the run state
  in ``room.configuration["linto"]``;
- ``teardown`` — service-account cleanup when a room ends with a run still active;
- ``studio_token_for`` — the identity bridge: the Studio JWT (+ organization)
  the browser SDK acts with, from the configured source (shared service account
  today, the user's own LinTO key through the studio-api identity exchange).

The native bot republishes the live captions INTO the LiveKit room as
transcription segments, so every participant sees them via the native overlay —
no per-viewer Studio socket. ``_login`` / ``resolve_conversation_id`` stay for
the summary Celery task and the teardown, which run under the service account.
"""

import base64
import json
import time
from datetime import datetime, timezone
from logging import getLogger

from django.conf import settings
from django.core.cache import cache

import requests

from core import models
from core.api.permissions import get_recording_permission_level
from core.entitlements.capabilities import recording_entitled
from core.recording.worker.factories import get_worker_service
from core.recording.worker.mediator import WorkerServiceMediator
from core.services.room_management import RoomManagement
from core.services.subtitle import SubtitleService
from core.utils import generate_bot_join_token

logger = getLogger(__name__)

DEFAULT_TIMEOUT = 15

# ``LINTO_STUDIO_TOKEN_SOURCE`` values — where the browser's Studio JWT comes from.
TOKEN_SOURCE_SERVICE_ACCOUNT = "service_account"  # noqa: S105
TOKEN_SOURCE_USER_KEY = "user_key"  # noqa: S105


def effective_token_source():
    """Where the browser's Studio token comes from, once the kill switch is
    applied: ``LINTO_STUDIO_TOKEN_SOURCE``, forced to the service account when
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
# start: the browser reads these keys, calls studio-api for the finalized
# captions and the "before you arrived" summary. They expose nothing that
# ``GET /rooms/{id}/`` does not already return in ``configuration["linto"]``.
ROOM_METADATA_SESSION_ID_KEY = "linto_transcription_session_id"
ROOM_METADATA_CHANNEL_ID_KEY = "linto_transcription_channel_id"
ROOM_METADATA_CHANNEL_INDEX_KEY = "linto_transcription_channel_index"
ROOM_METADATA_ORG_ID_KEY = "linto_transcription_org_id"
ROOM_METADATA_STARTED_AT_KEY = "linto_transcription_started_at"

# A Meet room drives ONE quickMeeting channel, always at index 0 — the index (not
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
]


class BotTranscriptionException(Exception):
    """Raised when a transcription-bot operation fails."""


class PermissionDeniedError(BotTranscriptionException):
    """Raised when the caller may not start/stop the transcription bot.

    The organizer's recording permissions (``transcript_permission`` for the
    bot itself, ``screen_recording_permission`` for the ``record`` add-on) and
    the who-can-stop rule (starter OR room admin/owner) are enforced
    server-side. The viewset answers 403 with ``code='permission_denied'``.
    """


class BotTranscriptionService:
    """Start/stop/poll a Studio quick-meeting bot for a Meet room."""

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

        After ``stop_bot`` DELETEs the quickMeeting with a unique ``&name=``,
        Studio's ``storeQuickMeetingFromStop`` stores a conversation under that
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
        catch-up) and LiveKit replays them to late joiners. When ``linto`` (the
        run state persisted by :meth:`mark_started`) is given, the Studio ids and
        the start timestamp ride along so a LATE JOINER can hydrate the
        transcript so far. Best-effort: a metadata failure must never break
        start/stop.
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

    # ── Browser-first lifecycle hooks ────────────────────────────────────
    # In the browser-first model the FRONTEND owns the Studio control plane via
    # the JS SDK (as the real user): it creates the quickMeeting, chooses the
    # profile/translations, and starts/stops the bot. The Meet backend only does
    # what the browser cannot: mint the LiveKit bot join token (needs Meet's
    # LiveKit secret), gate on the organizer's permissions, drive the room-wide
    # banner + native-subtitle takeover, the optional video egress, and the
    # autonomous post-meeting summary. Room state lives in
    # ``room.configuration["linto"]`` exactly as before.

    def prepare(self, room, channel_id, user=None):
        """Gate + mint the native bot join token for a browser-launched session.

        Enforces the organizer's ``transcript_permission`` (raises
        :class:`PermissionDeniedError`), stops the native subtitle agent on
        takeover, and returns the room-scoped LiveKit join token the browser
        injects into ``session.meta.native`` before starting the bot. No state is
        persisted yet (the session may still fail to start browser-side).
        """
        if not self._has_recording_permission(
            room, user, models.RecordingModeChoices.TRANSCRIPT
        ):
            raise PermissionDeniedError(
                "the organizer restricts transcription to administrators/owners"
            )
        # LinTO takes over the captions: stop the native subtitle agent (if any).
        self._stop_native_subtitles(room)
        token = generate_bot_join_token(str(room.id), str(channel_id))
        return {
            "token": token,
            "livekit_url": settings.LINTO_NATIVE_LIVEKIT_URL,
            "room": str(room.id),
        }

    def mark_started(self, room, data, user=None):
        """Record a browser-started run and light the room-wide state.

        ``data`` carries the Studio ids the browser obtained via the SDK
        (``session_id``, ``channel_id``, ``org_id``, ``bot_id``) plus the add-ons
        (``summary`` default True, ``record`` default False). Re-checks the
        transcript permission (defence in depth), starts the optional video
        egress when allowed, persists ``room.configuration["linto"]`` and sets the
        banner. Returns the persisted state.
        """
        if not self._has_recording_permission(
            room, user, models.RecordingModeChoices.TRANSCRIPT
        ):
            raise PermissionDeniedError(
                "the organizer restricts transcription to administrators/owners"
            )
        summary = bool(data.get("summary", True))
        record = bool(data.get("record", False))
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

        linto = {
            "summary": summary,
            "record": record,
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

        room.configuration = {**(room.configuration or {}), "linto": linto}
        room.save(update_fields=["configuration"])
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

    def mark_stopped(
        self,
        room,
        data=None,
        user=None,
        enforce_permission=True,
        force_studio_delete=False,
    ):
        """Clear a run: banner off, stop egress, enqueue the summary, drop state.

        The STARTER's browser has already stopped the bot + quick session via the
        SDK (and passes the ``conversation_name`` it used on the DELETE). When the
        stopper is NOT the starter (an admin stopping someone else's run) or on
        teardown (``force_studio_delete``), the run holds no browser-side SDK
        session, so we service-account-delete the Studio bot + quick session here
        instead. Who may stop: the STARTER or a room admin/owner. Idempotent.
        """
        data = data or {}
        linto = (room.configuration or {}).get("linto")
        if not linto:
            return {"status": "idle"}
        is_starter = False
        if enforce_permission:
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
        self._set_banner(room, False)
        recording_id = linto.get("recording_id")
        if recording_id:
            self._stop_recording(recording_id)

        summary = bool(linto.get("summary", True))
        org_id = linto.get("org_id")
        session_id = linto.get("session_id")
        # The browser passes the unique name it used on the quickMeeting DELETE;
        # fall back to a deterministic one if absent (keeps the task resolvable).
        conversation_name = data.get("conversation_name") or (
            f"linto-{room.id}-{int(time.time())}"
        )
        # The starter tore the run down browser-side; anyone else (or teardown)
        # did not, so stop the Studio bot + session under the service account.
        if force_studio_delete or not is_starter:
            self._delete_studio_session(linto, conversation_name)
        summary_payload = None
        if summary and org_id and session_id:
            summary_payload = {
                "org_id": org_id,
                "session_id": session_id,
                "conversation_name": conversation_name,
                "recipient_user_id": linto.get("user_id"),
                "record": bool(linto.get("record", False)),
                "steps": {},
                "attempts": 0,
            }
        config = {k: v for k, v in (room.configuration or {}).items() if k != "linto"}
        if summary_payload is not None:
            config["linto_summary"] = summary_payload
        room.configuration = config
        room.save(update_fields=["configuration"])

        if summary_payload is not None:
            self._enqueue_summary(room)
        logger.info("LinTO run stopped for room %s (summary=%s)", room.id, summary)
        return {"status": "stopped"}

    def _delete_studio_session(self, linto, conversation_name):
        """SERVICE-ACCOUNT delete of the Studio bot + quick session in ``linto``.

        Best-effort and idempotent (a 404 from an already-stopped run is fine).
        Used when the caller cannot stop the run browser-side via the SDK: an
        admin stopping someone else's run, or the ``room_finished`` teardown.
        ``conversation_name`` is passed to the quickMeeting DELETE so the summary
        task can still resolve the finalized conversation.
        """
        org_id = linto.get("org_id")
        session_id = linto.get("session_id")
        bot_id = linto.get("bot_id")
        if not (org_id and (bot_id or session_id)):
            return
        try:
            headers = self._headers()
            if bot_id is not None:
                requests.delete(
                    f"{self.studio_base}/api/organizations/{org_id}/bots/{bot_id}",
                    headers=headers,
                    timeout=DEFAULT_TIMEOUT,
                )
            if session_id:
                requests.delete(
                    f"{self.studio_base}/api/organizations/{org_id}/quickMeeting/{session_id}",
                    headers=headers,
                    params={"force": "true", "name": conversation_name},
                    timeout=DEFAULT_TIMEOUT,
                )
        except (requests.RequestException, BotTranscriptionException) as exc:
            logger.warning("LinTO service-account session delete: %s", exc)

    def teardown(self, room):
        """Best-effort cleanup when a room ends with a run still active.

        The browser may have closed without stopping. Delegates to
        ``mark_stopped`` (no permission gate), which service-account-deletes the
        Studio bot + quick session, clears the banner/egress and enqueues the
        summary.
        """
        if not (room.configuration or {}).get("linto"):
            return
        self.mark_stopped(room, {}, enforce_permission=False, force_studio_delete=True)

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

    # ── Studio token for the browser SDK (identity bridge) ───────────────
    def studio_token_for(self, user):
        """The Studio JWT + context the browser SDK acts with, for ``user``.

        The Meet backend is the identity bridge between the meeting and LinTO:
        it knows the participant (``sub``/``email`` from the LiveKit room token)
        and hands the browser a Studio token from ``LINTO_STUDIO_TOKEN_SOURCE``:

        - ``service_account`` — the shared service account, acting in
          ``LINTO_STUDIO_DEFAULT_ORG_ID`` (or its first organization). One Studio
          identity for the whole instance ⇒ one live transcription at a time.
        - ``user_key`` — the user's OWN LinTO API key, through the studio-api
          identity exchange (``POST /api/auth/external/token``); the
          organization is the key's. No per-instance limit.

        With the per-user feature system switched off
        (``LINTO_ENTITLEMENTS_ENABLED=False``) the token always comes from the
        service account: there is no per-user key without an entitlement.

        Returns ``{"enabled": True, "token", "base_url", "organization_id",
        "expires_in", "capabilities"}``, or ``{"enabled": False, "reason"}`` when
        the option is not active for this user. Raises
        :class:`BotTranscriptionException` on misconfiguration / Studio failure.
        The browser never receives a long-lived key: ``expires_in`` (seconds,
        ``None`` = unknown) lets it refresh before expiry.
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

    def _browser_base(self):
        return settings.LINTO_STUDIO_BROWSER_API_URL or settings.LINTO_STUDIO_BASE_URL

    def _service_account_token(self):
        token = self._login()
        headers = {"Authorization": f"Bearer {token}"}
        return {
            "enabled": True,
            "token": token,
            "base_url": self._browser_base(),
            "organization_id": self._service_account_org(headers),
            "expires_in": jwt_seconds_left(token),
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
                "base_url": self._browser_base(),
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


def jwt_seconds_left(token):
    """Seconds until ``token`` (a JWT) expires, ``None`` if unknown.

    Reads the unverified payload only — the token was minted by Studio and is
    handed back to the browser as-is; this is purely a refresh hint.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
    except (AttributeError, IndexError, ValueError, TypeError):
        return None
    if not isinstance(exp, (int, float)):
        return None
    return max(0, int(exp - time.time()))
