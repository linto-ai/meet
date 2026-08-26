"""Drive a LinTO Studio quick-meeting transcription bot for a Meet room.

The in-room "LinTO" tool starts/stops a bot that joins THIS Meet room as a
hidden LiveKit participant, streams the participants' audio to the LinTO
Transcriber and republishes the live captions INTO the room as LiveKit
transcription segments (rendered by the native caption overlay + the LinTO
panel). We talk to the Studio control plane EXACTLY as the Studio UI does —
POST /api/organizations/{org}/quickMeeting + /bots, DELETE quickMeeting on stop —
so a finalized Studio conversation + LLM summary is produced when the session
ends. Session-API (unauthenticated on the internal net) is only read to hydrate
the finalized captions for a participant who opens the panel mid-transcription.

Everything Studio-side is isolated in this service: the viewset only knows
BotTranscriptionService, never the Studio HTTP contract.
"""

import time
from logging import getLogger

from django.conf import settings

import requests
from asgiref.sync import async_to_sync

from core import models
from core.api.permissions import get_recording_permission_level
from core.recording.worker.factories import get_worker_service
from core.recording.worker.mediator import WorkerServiceMediator
from core.services.room_management import RoomManagement
from core.services.subtitle import SubtitleService
from core.utils import generate_bot_join_token

logger = getLogger(__name__)

DEFAULT_TIMEOUT = 15

# LiveKit room-metadata key lit while a LinTO bot transcribes the room. Read by
# EVERY participant's frontend (banner, CC badge, panel state) — the shared
# source of truth, replayed to late joiners by LiveKit itself.
ROOM_METADATA_STATUS_KEY = "linto_transcription_status"
ROOM_METADATA_STARTER_KEY = "linto_transcription_started_by"


class BotTranscriptionException(Exception):
    """Raised when a transcription-bot operation fails."""


class NoQuickMeetingProfile(BotTranscriptionException):
    """Raised when an org has no quickMeeting ASR profile and no default is set.

    Distinct from generic upstream failures so the viewset can answer 409 with a
    machine code (``no_quickmeeting_profile``) instead of a generic 502.
    """


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
        self.session_api = (settings.LINTO_SESSION_API_URL or "").rstrip("/")
        self._token = None

    # ── auth ─────────────────────────────────────────────────────────────
    def _login(self):
        """Service auth to Studio. Prefers a login (BOT+MICROPHONE rights via the
        account's org), falls back to a static LINTO_STUDIO_API_TOKEN."""
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

    # ── resolution (dynamic, no hard-coded org/profile) ──────────────────
    def _resolve_org(self, headers):
        if settings.LINTO_STUDIO_DEFAULT_ORG_ID:
            return settings.LINTO_STUDIO_DEFAULT_ORG_ID
        r = requests.get(
            f"{self.studio_base}/api/organizations/",
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        data = r.json() if r.status_code == 200 else []
        orgs = data if isinstance(data, list) else data.get("organizations", [])
        if not orgs:
            raise BotTranscriptionException(
                "no Studio organization available for the bot"
            )
        # Prefer a "Visio" org if present, else the first one.
        visio = [o for o in orgs if "visio" in (o.get("name", "") or "").lower()]
        chosen = visio[0] if visio else orgs[0]
        return chosen.get("_id") or chosen.get("id")

    def _resolve_profile(self, headers, org_id, config):
        pid = config.get("asr_profile_id") or settings.LINTO_STUDIO_DEFAULT_PROFILE_ID
        if pid:
            return pid
        r = requests.get(
            f"{self.studio_base}/api/organizations/{org_id}/transcriber_profiles",
            headers=headers,
            params={"quickMeeting": "true"},
            timeout=DEFAULT_TIMEOUT,
        )
        profiles = r.json() if r.status_code == 200 else []
        if isinstance(profiles, dict):
            profiles = (
                profiles.get("transcriber_profiles") or profiles.get("list") or []
            )
        if not profiles:
            raise NoQuickMeetingProfile(
                "no quickMeeting ASR profile in the org — create one in Studio or "
                "set LINTO_STUDIO_DEFAULT_PROFILE_ID"
            )
        return profiles[0].get("id") or profiles[0].get("_id")

    def _fetch_profiles(self, headers, org_id):
        """Raw quickMeeting ASR profiles for an org (list)."""
        r = requests.get(
            f"{self.studio_base}/api/organizations/{org_id}/transcriber_profiles",
            headers=headers,
            params={"quickMeeting": "true"},
            timeout=DEFAULT_TIMEOUT,
        )
        profiles = r.json() if r.status_code == 200 else []
        if isinstance(profiles, dict):
            profiles = (
                profiles.get("transcriber_profiles") or profiles.get("list") or []
            )
        return profiles or []

    def list_profiles(self, org_id, headers=None):
        """Panel-facing quickMeeting ASR profiles: id/name/languages/translations."""
        headers = headers or self._headers()
        result = []
        for prof in self._fetch_profiles(headers, org_id):
            config = prof.get("config") or {}
            languages = []
            for lang in config.get("languages") or []:
                code = lang.get("candidate") if isinstance(lang, dict) else lang
                if code:
                    languages.append(code)
            # availableTranslations may be a flat list (["en","de"]) OR a dict
            # ({"discrete": ["de"], "external": [...]}) — flatten either shape.
            raw_tr = (
                prof.get("translations")
                or config.get("availableTranslations")
                or config.get("translations")
                or []
            )
            if isinstance(raw_tr, dict):
                translations = [
                    t for grp in raw_tr.values() if isinstance(grp, list) for t in grp
                ]
            else:
                translations = list(raw_tr)
            result.append(
                {
                    "id": prof.get("id") or prof.get("_id"),
                    # Display name lives in config.name; the top-level name is null.
                    "name": prof.get("name") or config.get("name") or "",
                    "languages": languages,
                    "translations": translations,
                }
            )
        return result

    def list_profiles_for_room(self, room):  # pylint: disable=unused-argument
        """Resolve the room's Studio org, then list its quickMeeting ASR profiles."""
        headers = self._headers()
        org_id = self._resolve_org(headers)
        return self.list_profiles(org_id, headers=headers)

    def _room_owner(self, room):
        """The room's OWNER user, if any (for anonymous-triggered egress ownership)."""
        access = (
            room.accesses.filter(role=models.RoleChoices.OWNER)
            .select_related("user")
            .first()
        )
        return access.user if access else None

    def _persist_session_meta(self, org_id, session_id, meta, headers):
        """Persist the (token-bearing) session meta before the bot is dispatched.

        The native join token can only be minted once the Studio channel_id exists
        (it is created by the quickMeeting POST), so it is written back onto the
        session here via PATCH, ahead of the /bots dispatch the Scheduler reacts to.
        The token is NEVER logged (confidentiality); on failure the caller degrades
        to a token-less native declaration (→ Scheduler demotes to the web bot).

        Raises on a non-2xx response so the caller's best-effort guard engages.
        """
        r = requests.patch(
            f"{self.studio_base}/api/organizations/{org_id}/sessions/{session_id}",
            headers=headers,
            json={"meta": meta},
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code not in (200, 201, 204):
            raise BotTranscriptionException(
                f"session meta update failed: {r.status_code} {r.text[:200]}"
            )

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

    # ── lifecycle ────────────────────────────────────────────────────────
    def _room_url(self, room):
        base = (settings.MEET_PUBLIC_URL or "").rstrip("/")
        slug = getattr(room, "slug", None) or str(room.id)
        return f"{base}/{slug}"

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

    def _set_banner(self, room, active, user=None):
        """Light/clear the room-wide "transcription in progress" state.

        Writes a DEDICATED LiveKit room-metadata key (independent of the egress
        ``recording_mode``/``recording_status`` machinery, so both can coexist).
        Every participant's frontend reads it (banner, CC badge, panel state) and
        LiveKit replays it to late joiners. Best-effort: a metadata failure must
        never break start/stop.
        """
        try:
            if active:
                # Same id as ``configuration["linto"]["user_id"]`` / bot-status so
                # every participant's frontend can compute "started by me".
                starter = (
                    str(user.id)
                    if (user is not None and getattr(user, "is_authenticated", False))
                    else ""
                )
                async_to_sync(RoomManagement().update_metadata)(
                    str(room.id),
                    {
                        ROOM_METADATA_STATUS_KEY: "active",
                        ROOM_METADATA_STARTER_KEY: starter,
                    },
                )
            else:
                async_to_sync(RoomManagement().update_metadata)(
                    str(room.id),
                    {},
                    [ROOM_METADATA_STATUS_KEY, ROOM_METADATA_STARTER_KEY],
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

    def start_bot(self, room, config, user=None):  # noqa: PLR0915
        """Start LinTO live transcription on THIS room (always), plus add-ons.

        Clicking the LinTO tool ALWAYS starts a Studio quick-meeting bot with
        native real-time subtitles and per-stream diarization. Two freely-
        combinable add-ons ride on top:
        - ``summary`` (default ON) → an autonomous live-summary task summarizes
          the bot's OWN finalized conversation and delivers it on stop.
        - ``record`` (default OFF) → a standalone LiveKit video egress.
        """
        # Idempotency guard: a prior bot/recording must be torn down before a new
        # one is started, otherwise its session/bot/egress ids get overwritten in
        # room.configuration and leak (orphaned bot kept transcribing, un-finalized
        # Studio session). A double start-bot (double-click / second tab) is safe.
        if (room.configuration or {}).get("linto"):
            logger.info(
                "LinTO bot already running for room %s — stopping it first", room.id
            )
            self.stop_bot(room, enforce_permission=False)

        summary = bool(config.get("summary", True))
        record = bool(config.get("record", False))

        # Organizer recording permissions — enforced SERVER-side (never trust the
        # UI): the live transcription is gated by transcript_permission; the
        # optional video-record add-on by screen_recording_permission. A denied
        # transcription aborts (403); a denied record is silently dropped.
        if not self._has_recording_permission(
            room, user, models.RecordingModeChoices.TRANSCRIPT
        ):
            raise PermissionDeniedError(
                "the organizer restricts transcription to administrators/owners"
            )
        if record and not self._has_recording_permission(
            room, user, models.RecordingModeChoices.SCREEN_RECORDING
        ):
            logger.warning(
                "LinTO: record requested without screen_recording permission for "
                "room %s — dropping it",
                room.id,
            )
            record = False

        # LinTO takes over the captions: stop the native subtitle agent (if any)
        # so the overlay does not show two transcriptions of the same speech.
        self._stop_native_subtitles(room)

        headers = self._headers()
        org_id = self._resolve_org(headers)
        profile_id = self._resolve_profile(headers, org_id, config)

        channel = {
            "name": "Main",
            "transcriberProfileId": profile_id,
            "enableLiveTranscripts": True,
            "diarization": bool(config.get("diarization", True)),
            # Live transcription is always on and TEXT-only: the autonomous summary
            # task reads the finalized conversation text (get_media), never stored
            # audio, so we never ask Studio to keep the audio.
            "keepAudio": False,
            # Same wire format the Studio UI uses (QuickSessionCreateContent.vue):
            # a list of target translation language codes.
            "translations": config.get("translations") or [],
        }
        # PHASE 2: hand the native LiveKit agent what it needs to join the room.
        # room.id == the LiveKit room name (see meet subtitle.py). Meet OWNS this
        # room's LiveKit credentials and mints the bot's per-room join token itself
        # (generate_bot_join_token) so the agent stays credential-agnostic — the
        # signing secret never leaves Meet. The token is added below, once the
        # Studio channel_id (part of the bot identity) is known, then persisted onto
        # the session meta before the bot is dispatched.
        #
        # meta shape (SHARED CONTRACT): the generic capability map ``native`` plus
        # a back-compat ``linto_native`` alias (same object) kept ONE release.
        meta = {}
        if settings.LINTO_VISIO_NATIVE_ENABLED:
            native_descriptor = {
                "livekitUrl": settings.LINTO_NATIVE_LIVEKIT_URL,
                "room": str(room.id),
            }
            meta["native"] = {"visio-native": native_descriptor}
            meta["linto_native"] = native_descriptor
        qm = requests.post(
            f"{self.studio_base}/api/organizations/{org_id}/quickMeeting/",
            headers=headers,
            json={"channels": [channel], "meta": meta},
            timeout=DEFAULT_TIMEOUT,
        )
        if qm.status_code not in (200, 201):
            raise BotTranscriptionException(
                f"quickMeeting create failed: {qm.status_code} {qm.text[:300]}"
            )
        session = qm.json() or {}
        session_id = session.get("id")
        channel_id = (session.get("channels") or [{}])[0].get("id")
        if not session_id or not channel_id:
            raise BotTranscriptionException(
                f"quickMeeting response missing ids: {session}"
            )

        # Now that the Studio-generated channel_id is known, mint the per-room join
        # token (identity carries room + channel to avoid cross-channel eviction) and
        # persist it onto the session meta BEFORE the bot is dispatched, so the
        # Scheduler reads a usable credential. Best-effort: if minting or the meta
        # update fails, the session keeps its native declaration WITHOUT a token, and
        # the Scheduler's capability gate deterministically demotes it to the web bot
        # rather than pinning it to a native replica it cannot authenticate against.
        if settings.LINTO_VISIO_NATIVE_ENABLED and meta.get("native"):
            try:
                native_descriptor["token"] = generate_bot_join_token(
                    str(room.id), channel_id
                )
                # linto_native is the SAME object → the alias carries the token too.
                self._persist_session_meta(org_id, session_id, meta, headers)
            except Exception:
                # Do NOT log the token or meta body (confidentiality).
                logger.warning(
                    "LinTO native: token mint/persist failed for session %s — "
                    "session stays web-eligible",
                    session_id,
                    exc_info=True,
                )

        bot_payload = {
            "url": self._room_url(room),
            "channelId": channel_id,
            "provider": settings.LINTO_BOT_PROVIDER,
            "enableDisplaySub": False,
            "subSource": "original",
        }
        bot = requests.post(
            f"{self.studio_base}/api/organizations/{org_id}/bots",
            headers=headers,
            json=bot_payload,
            timeout=DEFAULT_TIMEOUT,
        )
        if bot.status_code not in (200, 201):
            # Roll back the session so we don't litter Studio with a bot-less one.
            try:
                requests.delete(
                    f"{self.studio_base}/api/organizations/{org_id}/quickMeeting/{session_id}",
                    headers=headers,
                    params={"force": "true", "trash": "true"},
                    timeout=DEFAULT_TIMEOUT,
                )
            except requests.RequestException:
                pass
            raise BotTranscriptionException(
                f"bot start failed: {bot.status_code} {bot.text[:300]}"
            )

        linto = {
            "summary": summary,
            "record": record,
            "org_id": org_id,
            "session_id": session_id,
            "channel_id": channel_id,
            "bot_id": (bot.json() or {}).get("id"),
            # Recipient of the autonomous summary delivery: the authenticated
            # caller if any (else the live-summary task falls back to the room
            # owner). Stored as a plain id so the Celery task can re-load the user.
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

        # Light the room-wide "transcription in progress" state for everyone.
        self._set_banner(room, True, user=user)

        logger.info(
            "LinTO bot started for room %s: summary=%s record=%s session=%s",
            room.id,
            summary,
            record,
            linto.get("session_id"),
        )
        return self._status_payload(linto, status="running")

    def stop_bot(self, room, user=None, enforce_permission=True):  # noqa: PLR0912
        """Stop the bot by ending the quick session → finalizes the conversation.

        When ``summary`` was requested, a UNIQUE conversation name is passed to
        the quickMeeting DELETE (Studio's ``storeQuickMeetingFromStop`` uses it as
        the conversation name), persisted in ``room.configuration["linto_summary"]``,
        and the autonomous ``process_bot_live_summary`` Celery task is enqueued.

        Who may stop (``enforce_permission``, default True): the STARTER (matched
        by the stored ``user_id``) or a room admin/owner — so a random participant
        cannot stop someone else's transcription. ``enforce_permission=False`` is
        used for the internal idempotency teardown in ``start_bot``.
        """
        linto = (room.configuration or {}).get("linto")
        if not linto:
            return {"status": "idle"}
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
        # Clear the room-wide "transcription in progress" state first so every
        # participant's UI flips promptly, whatever the Studio teardown does.
        self._set_banner(room, False)
        org_id = linto.get("org_id")
        session_id = linto.get("session_id")
        summary = bool(linto.get("summary", True))
        # Unique, findable conversation name for the live-summary task to resolve.
        conversation_name = f"linto-{room.id}-{int(time.time())}"
        if org_id and session_id:
            headers = self._headers()
            bot_id = linto.get("bot_id")
            # Stop the bot EXPLICITLY first: humans are still in the room, so the
            # empty-meeting auto-leave never arms — only an explicit stopbot makes it
            # leave promptly (mirrors purge_quick_sessions: DELETE bot, then session).
            if bot_id is not None:
                try:
                    requests.delete(
                        f"{self.studio_base}/api/organizations/{org_id}/bots/{bot_id}",
                        headers=headers,
                        timeout=DEFAULT_TIMEOUT,
                    )
                except requests.RequestException as exc:
                    logger.warning("LinTO bot stop: bot delete error: %s", exc)
            # DELETE the quick session (force) → Studio drains captions, ends the
            # session, and stores the finalized conversation (storeQuickMeetingFromStop).
            # The &name= makes the resulting conversation findable by the summary task.
            try:
                r = requests.delete(
                    f"{self.studio_base}/api/organizations/{org_id}/quickMeeting/{session_id}",
                    headers=headers,
                    params={"force": "true", "name": conversation_name},
                    timeout=DEFAULT_TIMEOUT,
                )
                if r.status_code not in (200, 201, 204):
                    logger.warning(
                        "LinTO bot stop: quickMeeting delete returned %s %s",
                        r.status_code,
                        r.text[:200],
                    )
            except requests.RequestException as exc:
                logger.warning("LinTO bot stop error: %s", exc)
        # Stop the standalone video egress, if one was started.
        recording_id = linto.get("recording_id")
        if recording_id:
            self._stop_recording(recording_id)

        # Hand off the autonomous live-summary delivery. Persist everything the
        # task needs in a SEPARATE key so the active-bot guard (which keys on
        # "linto") never collides with a pending summary.
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
            try:
                # Local import avoids a module-level cycle (tasks.linto imports
                # this service lazily inside the task body).
                from core.tasks.linto import (  # noqa: PLC0415
                    process_bot_live_summary,
                )

                process_bot_live_summary.delay(str(room.id))
            except Exception as exc:
                logger.exception(
                    "LinTO bot stop: failed to enqueue live summary for room %s: %s",
                    room.id,
                    exc,
                )

        logger.info("LinTO bot stopped for room %s (summary=%s)", room.id, summary)
        return {"status": "stopped"}

    def bot_status(self, room):
        """Current bot status + live captions (polled by the panel)."""
        linto = (room.configuration or {}).get("linto")
        if not linto:
            return {"status": "idle", "captions": []}
        return self._status_payload(linto, status="running", with_captions=True)

    # ── helpers ──────────────────────────────────────────────────────────
    def _status_payload(self, linto, *, status, with_captions=False):
        payload = {
            "status": status,
            "session_id": linto.get("session_id"),
            "channel_id": linto.get("channel_id"),
            "org_id": linto.get("org_id"),
            "summary": linto.get("summary", True),
            "record": linto.get("record", False),
            # The starter's user id lets ANY participant's panel compute
            # "started by me" / "may I stop".
            "user_id": linto.get("user_id"),
            "captions": [],
        }
        if with_captions:
            payload["captions"] = self._captions(linto.get("session_id"))
        return payload

    def _captions(self, session_id):
        """Finalized closed-captions of the session, straight from Session-API.

        Used to HYDRATE a participant who opens the panel mid-transcription: the
        live feed itself reaches every browser as LiveKit transcription segments
        published by the bot. ``segment_id`` matches the bot's segment ids
        (``linto:<segmentId>``) so the frontend can merge both sources by id.
        """
        if not session_id or not self.session_api:
            return []
        try:
            r = requests.get(
                f"{self.session_api}/sessions/{session_id}",
                params={"withCaptions": "true"},
                timeout=DEFAULT_TIMEOUT,
            )
            channels = (
                (r.json() or {}).get("channels") or [] if r.status_code == 200 else []
            )
            raw = (channels[0].get("closedCaptions") or []) if channels else []
        except (requests.RequestException, ValueError) as exc:
            logger.debug("LinTO bot caption poll failed: %s", exc)
            return []
        captions = []
        for c in raw:
            seg = c.get("segmentId")
            captions.append(
                {
                    "segment_id": f"linto:{seg}" if seg is not None else None,
                    "text": c.get("text") or "",
                    "locutor": c.get("locutor") or "",
                    "participant_id": c.get("participantId") or None,
                    "language": c.get("lang") or "",
                    "start": c.get("timestamp")
                    if c.get("timestamp") is not None
                    else c.get("start"),
                    "translations": c.get("translations") or {},
                }
            )
        return captions
