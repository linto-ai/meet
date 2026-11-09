# Upgrade

All instructions to upgrade this project from one release to the next will be
documented in this file. Upgrades must be run sequentially, meaning you should
not skip minor/major releases while upgrading (fix releases can be skipped).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For most upgrades, you just need to run the django migrations with
the following command inside your docker container:

`python manage.py migrate`

(Note : in your development environment, you can `make migrate`.)

## [Unreleased]


### LinTO fork: a live run's conversation follows LINTO_STUDIO_MEMBERS_RIGHT

The Studio conversation of a live run now gives the members of the
organization the right `LINTO_STUDIO_MEMBERS_RIGHT` (0 in prd and stg: none),
as the deferred transcription does. It used to give them read and comment:
Studio applies that to any non-private session without `meta.membersRight`.

- Studio must apply `meta.membersRight` to the conversation of a stopped
  session (Studio `master` since 2026-09-22, `next`). On an older Studio the
  members keep read and comment.
- Nothing to set on the Meet side. The conversations of the runs before this
  release keep their rights: change them in Studio if needed.


### LinTO fork: the backend runs the live transcription, the Studio key stays on the server

The live transcription panel no longer drives Studio from the browser with a
token the backend handed it: `GET rooms/{id}/linto/studio-token/` is gone, and
with it the Studio key (`LINTO_STUDIO_API_TOKEN` in `service_account` mode) that
any logged-in participant could read in the browser. The backend lists the
profiles, creates the room's Studio session, launches and stops the bot itself
(`linto/profiles`, `linto/start`, `linto/stop` replace `linto/prepare`,
`linto/started`, `linto/stopped`). Each room has a session of its own,
`@meet-<room id>`, created through `POST /api/organizations/{org}/sessions/`
instead of a quickMeeting: several rooms now transcribe at the same time under
the same key.

- In the Studio organization the key acts in (`linagora` on meet.linagora.com):
  - the key's user needs the Meeting Manager role. Never Maintainer, which
    reads every conversation of the organization;
  - the organization needs the SESSION and BOT permissions;
  - if the organization sets a minimal security level, the live ASR profile
    (`LINTO_STUDIO_DEFAULT_PROFILE_ID`, else the first one) must reach it:
    `POST /sessions` checks it, the quickMeeting did not;
  - on a Studio with the SaaS plugin (`linto-saas`), the organization's plan
    needs the `live.sessions` capability. Otherwise the creation answers
    402/403 and the panel says that LinTO refused the transcription;
  - a Meeting Manager may not create a root folder: an admin creates the
    `LINTO_VISIO_FOLDER_NAME` folder (`Visio`) once, public, in the
    organization. Without it the conversations stay outside any folder (the
    delivery logs a 403 and goes on).
- The bot is sent to `DJANGO_EMAIL_APP_BASE_URL/<room slug>` (set in prd and
  stg).
- With the per-user rights on (`user_key`), the backend acts with the
  participant's own key, and stops a run with its starter's key.
- The config no longer carries `native_livekit_url`, `visio_native_enabled`,
  `bot_provider` nor `live_keep_audio`; the variables stay, read by the backend.
- Deploy the backend and the frontend together: a tab opened before the
  upgrade must be reloaded.


### LinTO fork: the live run's audio in Twake Drive

A live transcription now asks Studio to keep the meeting audio (`keepAudio` on
the quickMeeting channel, compressed to MP3 by the Transcriber since the
channel is live), and the end-of-meeting delivery puts it in the Twake Drive
meeting folder as `{date} - Recording.ogg`, next to the transcript, like the
deferred transcription does. The audio is optional to the delivery: when Studio
has none, the transcript, the document and the email go out without it.

- New backend variable `LINTO_LIVE_KEEP_AUDIO` (default `True`). `False` keeps
  the former text-only runs (nothing of the audio stored in Studio).
- Studio side: the conversation's audio is read through
  `GET /api/conversations/{id}/media` with the backend's Studio credential, so
  studio-api must see the Transcriber's audio volume (it does on the LinTO
  deployments). With per-participant diarization (`BOT_PERSTREAM=true` on the
  native bot), the bot must advertise `mixedRecording` (VisioBotService from
  linto-studio-plugins `next`, 2026-09-02 on), otherwise the Transcriber
  demotes the run to the mixed path to record it.
- While such a run is active, every participant sees the "Recording in
  progress" indicator, as for a LiveKit recording. The backend advertises it
  in the room metadata (`linto_transcription_keep_audio`, cleared at the stop);
  a text-only run (`LINTO_LIVE_KEEP_AUDIO=False`) shows none. Nothing to set.
- Deploy the backend and the frontend together (the panel reads
  `live_keep_audio` from the config).


### LinTO fork: deploying the rebase on upstream 1.37.0

Changes to make in `lasuite-meet-deploy` (`configs/meet/meet/{prd,stg}`) when
this line replaces the one rebased on upstream 1.28.0:

- Remove `RECORDING_STORAGE_EVENT_ENABLE` and `RECORDING_ENABLE_STORAGE_EVENT_AUTH`
  from `values.yaml`, and `RECORDING_STORAGE_EVENT_TOKEN` from `secrets.yaml`:
  the S3 storage hook is gone (upstream v1.30.0 above).
- Keep `RECORDING_STORAGE_POLLING_ENABLED: True`. It is now a safety net
  behind the LiveKit `egress_ended` webhook, which finalizes recordings. Polling
  only picks up files written more than `RECORDING_STORAGE_POLLING_GRACE_SECONDS`
  ago (300 by default), and a recording is claimed atomically, so the webhook and
  polling never notify it twice.
- Check that LiveKit still posts its webhooks to
  `/api/v1.0/rooms/webhooks-livekit/` (set in `configs/livekit-meet/livekit/{prd,stg}`).
- Replace `RECORDING_ENCODING_ENABLED`, `RECORDING_ENCODING_WIDTH`,
  `RECORDING_ENCODING_HEIGHT`, `RECORDING_ENCODING_FRAMERATE` and
  `RECORDING_ENCODING_VIDEO_BITRATE_KBPS`, which no longer exist, with
  `RECORDING_ENCODING_DEFAULT_RESOLUTION: 720p` and
  `RECORDING_ENCODING_DEFAULT_PROFILE: talking_heads` (720p, 15 fps, 700 kbps;
  the former values were 720p, 15 fps, 600 kbps). Without them the default
  profile is `full`: 30 fps and 3000 kbps, twice the frames to encode on the
  egress worker and five times the file size.
- Migrations `0023` to `0030` run in the `migrate` job; `0027` builds an index
  concurrently (non-atomic migration).
- Deploy the backend and the frontend together: LiveKit tokens now come from
  `POST /api/v1.0/rooms/{id}/join/`, so a tab opened before the upgrade must be
  reloaded.


### LinTO fork: live transcription line merged

This line now also carries what ran on `linto-live` (the `v1.26-live` image of
dev-meet): the live transcription panel in the meeting, the per-feature rights
system (entitlements) and the reworked deferred transcription. No migration
comes with it: dev-meet has the same `0023` to `0026` as meet.linagora.com, and
`0030_merge_linto_and_upstream_migrations` still ends the chain.

New backend variables and their defaults:

- `LINTO_ENTITLEMENTS_ENABLED` (default `False`): the per-user rights system.
  Off by default: every LinTO feature is open to everyone (live and deferred
  transcription, summary, translation, recording) without asking Studio for any
  right, and the browser's Studio token comes from the service account whatever
  `LINTO_STUDIO_TOKEN_SOURCE` says. `True` turns the rights on; it goes with the
  `linto` `ENTITLEMENTS_BACKEND`, since with the default backend nobody holds a
  LinTO capability (a deferred transcription is refused with 403
  `no_entitlement`, the "transcribe" checkbox is hidden, no summary is made).
- `LINTO_RECORDING_ENTITLEMENT_ENABLED` (default `False`): when `True`, the
  video recording also needs the `recording` capability, on top of the
  per-room recording permission (admins and owner by default).
- `ENTITLEMENTS_BACKEND` (default
  `core.entitlements.backends.local.LocalEntitlementsBackend`, which knows
  nothing about LinTO), `ENTITLEMENTS_BACKEND_PARAMETERS` (`{}`),
  `ENTITLEMENTS_CACHE_TIMEOUT` (`300`). Studio's resolution is
  `core.entitlements.backends.linto.LintoEntitlementsBackend`.
- `LINTO_FEATURE_ENABLED` (default `False`): the live transcription panel.
  With it: `LINTO_STUDIO_BROWSER_API_URL`, `LINTO_STUDIO_TOKEN_SOURCE`
  (`service_account` or `user_key`), `LINTO_IDENTITY_PROVIDER` (`meet`),
  `LINTO_STUDIO_INTEGRATION_TOKEN` (secret), `LINTO_STUDIO_AUTH_EMAIL` and
  `LINTO_STUDIO_AUTH_PASSWORD`, `LINTO_STUDIO_DEFAULT_ORG_ID`,
  `LINTO_STUDIO_DEFAULT_PROFILE_ID`, `LINTO_STUDIO_TOKEN_CACHE_TTL` (`300`),
  `LINTO_BOT_PROVIDER` (`visio`), `LINTO_VISIO_NATIVE_ENABLED` (`True`),
  `LINTO_NATIVE_LIVEKIT_URL`, `LINTO_NATIVE_TOKEN_TTL` (`43200`).

The deferred transcription now logs in to Studio with
`LINTO_STUDIO_INTEGRATION_TOKEN`, else the `LINTO_STUDIO_AUTH_EMAIL` /
`LINTO_STUDIO_AUTH_PASSWORD` service account, else `LINTO_STUDIO_API_TOKEN`:
a deployment that only sets `LINTO_STUDIO_API_TOKEN` is unchanged.

Changes to make in `lasuite-meet-deploy`:

- meet.linagora.com (`configs/meet/meet/prd/values.yaml`): nothing to add.
  `LINTO_ENTITLEMENTS_ENABLED` defaults to `False`, which keeps the current
  behaviour (deferred transcription and summary for everyone, no right asked
  to Studio or Twake). Leave `LINTO_FEATURE_ENABLED`, `ENTITLEMENTS_BACKEND`
  and `LINTO_RECORDING_ENTITLEMENT_ENABLED` unset too: no live panel,
  recording governed by the room permission only.
- dev-meet (`configs/meet/meet/stg/values.yaml`): nothing to add. It already
  sets the live line's variables (`LINTO_FEATURE_ENABLED`,
  `LINTO_ENTITLEMENTS_ENABLED: True`, the `linto` entitlements backend,
  `LINTO_STUDIO_TOKEN_SOURCE: user_key`,
  `LINTO_RECORDING_ENTITLEMENT_ENABLED: False`, the service account, the
  native bot). Its image moves from `v1.26-live` to this line's, with the
  changes of the section above. It keeps its rights because it sets
  `LINTO_ENTITLEMENTS_ENABLED: True` explicitly.
- Any other instance that wants the per-user rights of Twake must set
  `LINTO_ENTITLEMENTS_ENABLED: True` (with the `linto` entitlements backend):
  without it, every LinTO feature is open to everyone.
- The LinTO endpoints (`prepare`, `started`, `stopped`, `studio-token`,
  `summary-services`, `transcription-languages`) now take the LiveKit room
  token in an `Authorization: X-LiveKit-Token <token>` header, no longer in the
  body or the query string: deploy the backend and the frontend together, and
  a tab opened before the upgrade must be reloaded.


### Room access tokens moved to POST /join/

LiveKit access tokens are no longer returned when reading a room. They are henceforth issued by a dedicated `POST
/api/v1.0/rooms/{id}/join/` endpoint, which receives the display name in its JSON body. Display names therefore no
longer appear in URLs and token retrieval by logged-in users is now protected against CSRF.

- `GET /api/v1.0/rooms/{id}/` no longer returns the `livekit` block, and neither do the list, create and update
responses
- The `username` field of `POST /api/v1.0/rooms/{id}/request-entry/` is now limited to 255 characters
- When the caller may enter the room directly, the response contains the room data plus a `livekit` block
- When the caller must go through the lobby, the response has no `livekit` block
- Session-authenticated callers must send the CSRF token (`X-CSRFToken` header and `csrftoken` cookie)
- Every join returning a `livekit` block mints a new token, valid for 6 hours, and anonymous callers get a new
participant identity each time.
- Joining again returns no `livekit` block to callers admitted through the lobby.
- Unregistered rooms are always entered directly: joining one always returns a `livekit` block.
- Unregistered rooms are no longer described with an `is_administrable` field,

### Unregistered rooms can no longer be named after a UUID

When `ALLOW_UNREGISTERED_ROOMS` is enabled, reading or joining a room whose identifier normalizes to an empty slug or to
a UUID now returns a 404 instead of describing or letting callers join the unregistered room.

This closes a lobby bypass since a registered LiveKit room is named after its room id. An identifier crafted to not
parse as a UUID, yet to normalize to an existing room's id, matched no registered room and was treated as an
unregistered one. The token minted for that unregistered room was valid for the registered room: anyone could join a
trusted or restricted room without being admitted through the lobby.

### LiveKit tokens for delegated users

The external API `POST /external-api/v1.0/rooms/{id}/livekit-token` now needs
the `rooms:livekit_token` scope (it needed `rooms:retrieve`). No migration grants
it: give it by hand to each application that mints LiveKit tokens.
Its tokens live one hour (`APPLICATION_LIVEKIT_TOKEN_TTL_SECONDS`), and
`start-recording` / `stop-recording` with a LiveKit token now need its holder
in the call.

### LiveKit room tokens name a user only through a signed proof

The endpoints authenticated by a LiveKit room token (`start-subtitle`,
`mute-participant`, `toggle-hand`, `rename`, and LinTO's `studio-token`,
`prepare`, `started`, `stopped`) no longer take the user from
the token identity: Meet now sets a `user_id` attribute and its
`user_id_proof` in the token of every authenticated user, and only that proof
names a user. A token minted before the upgrade has no proof, so on these
endpoints its holder is anonymous until they join the room again (reloading
the page is enough) or the token expires (six hours by default). The proof is
keyed by `DJANGO_SECRET_KEY`: all backend replicas must share it, and a
rotation has the same effect as the upgrade on the tokens already minted.

### Twake Drive instance from an OIDC claim

Twake Drive deliveries no longer build the instance as `{sub}.{TWAKE_INSTANCE_DOMAIN}`
when the `sub` holds an `@` or a dot. Such users get their instance from an
OIDC claim, stored at each sign-in. To set it up:

- Have the IdP return the claim at the userinfo endpoint of Meet's client.
  Meet reads userinfo only, not the ID token.
- The claim is `workplaceFqdn` by default. Set
  `OIDC_USERINFO_TWAKE_INSTANCE_FIELD` if your IdP names it differently.

Existing users get their instance at their next sign-in. Until then, for up to
`SESSION_COOKIE_AGE` (12 hours by default) after the upgrade, their uploads are
skipped with a warning.

## v1.37.0

### Crisp support chat replaced by the La Suite support form

The Crisp chat is gone, the support button, now open the La Suite support form. They are hidden unless these `FRONTEND_SUPPORT` keys are all set.
The former help links for recording/transcription are now redirecting to the adhoc page in the official documentation

### Pages hidden from search engines

Every page except `/` now carries a `noindex` tag and, with the provided nginx configurations, an `X-Robots-Tag` header to stay out of search indexes.
A new `robots.txt` also blocks the main AI crawlers. If you maintain your own nginx configuration, copy the `location = /` and `location = /home.html` blocks from `src/frontend/default.conf`, and the `X-Robots-Tag` header from its `location /`.

### LiveKit server v1.13.11 and egress v1.15.0

The development stack now runs LiveKit server v1.13.11 and LiveKit Egress v1.15.0. When upgrading your own deployment:

- Deploy this release of Meet first, then upgrade egress, then the server. Older Meet releases discard the partial recordings described below.
- Transcripts no longer go through Chrome. Since egress v1.13, audio-only recordings always use the egress SDK pipeline.
- A transcript now waits for actual audio: it stays "starting", and participants are asked to turn on their microphone, while every microphone is muted.
- Recordings cut short are now kept. Since egress v1.14, a transcript that lost the room, or any recording drained because its egress host ran out of CPU, still uploads what it recorded, even though egress may report it as failed. Meet saves and processes it as usual, and tells participants the recording was interrupted. A screen recording that loses the room is still reported as complete, and saved without a warning.
- Telephony: since server v1.13.8, a call that matches no dispatch rule, for example while no room is active, is rejected with a SIP 404 instead of being silently dropped.

## [v1.35.0]

### Purging deleted rooms

Deleting a room now only soft deletes it: it is hidden but keeps its slug and PIN code. The `purge_inactive_rooms` command also permanently deletes soft-deleted rooms. See [the room purge documentation](docs/features/room-purge.md).

- Inactive soft-deleted rooms are purged along with the other inactive rooms: if `ROOM_INACTIVITY_DELETION_DAYS` is already set, nothing else is needed.
- The new `ROOM_DELETED_RETENTION_DAYS` setting purges soft-deleted rooms that many days after their deletion, whether they are inactive or not. It is unset by default.
- As for inactive rooms, a deleted room holding a saved recording its users may still access is kept until that recording expires.

### Recording permission

We've introduced the ability for any authenticated participant to start a recording or transcription, but this behavior remains configurable.
In the LinTO fork, who may record is set per mode by `RECORDING_SCREEN_PERMISSION` and `RECORDING_TRANSCRIPT_PERMISSION` (`admin_owner` by default, or `authenticated`), and per room from the admin panel; `RECORDING_AUTHENTICATED_PARTICIPANTS_ENABLED` does not exist.

## [v1.34.0]

### Marketing / Brevo integration now uses `django-lasuite`

The in-house marketing service (`core.services.marketing`) has been removed and
replaced by the shared implementation from `django-lasuite`
(`lasuite.marketing`). This fixes a bug where updating a user's contact on
Brevo overwrote their list memberships, removing lists set by other
La Suite products. Existing lists are now preserved and merged.

**Celery worker required.** Newsletter signup on login
(`SIGNUP_NEW_USER_TO_MARKETING_EMAIL=True`) is now dispatched as an
asynchronous Celery task (`lasuite.marketing.tasks.create_or_update_contact`)
instead of a synchronous call with a 1s timeout. Make sure a Celery worker is
running alongside the backend, otherwise contacts will never be pushed to Brevo.

**Configuration changes.** The following environment variables / settings are
**removed** and no longer read:

- `MARKETING_SERVICE_CLASS`
- `BREVO_API_KEY`
- `BREVO_API_CONTACT_LIST_IDS`
- `BREVO_API_CONTACT_ATTRIBUTES` (previous default: `{"VISIO_USER": True}`)
- `BREVO_API_TIMEOUT`

They are replaced by a single `LASUITE_MARKETING` setting, configured through:

| Variable                       | Default                                          | Description                                  |
| ------------------------------ | ------------------------------------------------ | -------------------------------------------- |
| `LASUITE_MARKETING_BACKEND`    | `lasuite.marketing.backends.dummy.DummyBackend`  | Backend class path                           |
| `LASUITE_MARKETING_PARAMETERS` | `{}`                                             | Keyword arguments passed to the backend      |

⚠️ The default backend is now a **dummy** (no-op). If you previously used
Brevo, you must explicitly configure it, otherwise signups are silently dropped:

    LASUITE_MARKETING_BACKEND=lasuite.marketing.backends.brevo.BrevoBackend
    LASUITE_MARKETING_PARAMETERS={"api_key": "<your-brevo-api-key>", "api_contact_list_ids": [1, 2], "api_contact_attributes": {"VISIO_USER": True}}

Migration mapping:

- `BREVO_API_KEY` → `api_key`
- `BREVO_API_CONTACT_LIST_IDS` → `api_contact_list_ids`
- `BREVO_API_CONTACT_ATTRIBUTES` → `api_contact_attributes` (re-add
  `{"VISIO_USER": True}` if you relied on the old default)
- `BREVO_API_TIMEOUT` → no equivalent (the request runs in a background task)

Note: `BREVO_API_KEY` used to support being read from a secret file; the API key
now lives inside `LASUITE_MARKETING_PARAMETERS`, so adapt how you inject that
secret (e.g. build the whole variable from your secret store).

### Recording encoding settings replaced by a resolution/profile model

The `RECORDING_ENCODING_*` settings introduced in v1.16.0 exposed raw encoder
values (width, height, framerate, bitrate). They are replaced by two named and configurable sets of
dimensions, a **resolution** (default: `540p`, `720p`, `1080p`) and a **profile**
(default: `talking_heads`, `text`, `mixed`, `full`), which are resolved to the width, height,
fps and video bitrate.

**The following environment variables are no longer read. If they are still set in
your deployment they are silently ignored, and your recordings will be encoded with
the new defaults instead of your tuned values.**

| Removed variable                        | Replaced by                                                                                                   |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `RECORDING_ENCODING_ENABLED`            | Nothing. A default encoding is now always built (see below). **Not** `RECORDING_CUSTOM_ENCODING_ENABLED`, which gates a different feature. |
| `RECORDING_ENCODING_WIDTH`              | The `width` of the entry selected by `RECORDING_ENCODING_DEFAULT_RESOLUTION` in `RECORDING_ENCODING_AVAILABLE_RESOLUTIONS`. |
| `RECORDING_ENCODING_HEIGHT`             | The `height` of that same entry.                                                                              |
| `RECORDING_ENCODING_FRAMERATE`          | The `fps` of the profile selected by `RECORDING_ENCODING_DEFAULT_PROFILE` in `RECORDING_ENCODING_AVAILABLE_PROFILES`. |
| `RECORDING_ENCODING_VIDEO_BITRATE_KBPS` | That profile's `kbps`.                                                                  |

`RECORDING_ENCODING_AUDIO_BITRATE_KBPS` and `RECORDING_ENCODING_KEY_FRAME_INTERVAL_S`
keep their names and meaning. The keyframe interval now defaults to `0` (unset,
encoder's choice) instead of `4.0`.

#### If you never set `RECORDING_ENCODING_ENABLED=True`

The shipped defaults (`RECORDING_ENCODING_DEFAULT_PROFILE=full`,
`RECORDING_ENCODING_DEFAULT_RESOLUTION=720p`) match LiveKit's built-in
`H264_720P_30` preset: 1280×720, 30 fps, 3000 kbps H.264 MAIN, 128 kbps AAC.
Video output is therefore unchanged.

Audio and keyframing may not be. These values are now sent explicitly as advanced
`EncodingOptions` rather than relying on LiveKit's preset, so
`RECORDING_ENCODING_AUDIO_BITRATE_KBPS` and `RECORDING_ENCODING_KEY_FRAME_INTERVAL_S`
now apply to every recording. They previously applied only when
`RECORDING_ENCODING_ENABLED` was `True`. **If you set either of them while the
feature was disabled, they had no effect and now do**; check them before upgrading.

If you never set them, no action is required: 128 kbps AAC is what the preset used,
and the keyframe interval now defaults to `0`, which leaves the field unset so the
encoder keeps picking it as before. Set `RECORDING_ENCODING_KEY_FRAME_INTERVAL_S=4.0`
if you want fixed 4-second keyframes (the value the setting defaulted to while it
was gated behind `RECORDING_ENCODING_ENABLED`).

To keep letting LiveKit pick the encoding instead, set either default to an empty
value:

```
RECORDING_ENCODING_DEFAULT_RESOLUTION=
RECORDING_ENCODING_DEFAULT_PROFILE=
```

#### If you had tuned `RECORDING_ENCODING_*` values

Translate your old values into a default resolution and a default profile. Declare your own resolution and/or profile. Both maps are read from the
environment as a single-line Python/JSON dict literal (parsed with
`ast.literal_eval`, so use double-quoted keys and no trailing commas, and do not
add outer quotes in `.env`-style files):

```bash
RECORDING_ENCODING_AVAILABLE_RESOLUTIONS={"540p": {"width": 960, "height": 540}, "720p": {"width": 1280, "height": 720}, "1080p": {"width": 1920, "height": 1080}}
RECORDING_ENCODING_AVAILABLE_PROFILES={"my_old_profile": {"fps": 15, "kbps": {"540p": 350, "720p": 600, "1080p": 1100}}}
RECORDING_ENCODING_DEFAULT_RESOLUTION=720p
RECORDING_ENCODING_DEFAULT_PROFILE=my_old_profile
```

Both maps are validated at startup and a malformed one raises a `ValueError`:

- every entry of `RECORDING_ENCODING_AVAILABLE_RESOLUTIONS` must declare `width` and
  `height`, and every entry of `RECORDING_ENCODING_AVAILABLE_PROFILES` an `fps` and a
  `kbps` map;
- every profile must define a `kbps` entry for **exactly** the keys of
  `RECORDING_ENCODING_AVAILABLE_RESOLUTIONS`; overriding one of the two maps usually
  means overriding both;
- `RECORDING_ENCODING_DEFAULT_RESOLUTION` and `RECORDING_ENCODING_DEFAULT_PROFILE`,
  when non-empty, must be keys of their respective map.

#### Breaking: custom worker services must accept `encoding_options`

Only concerns deployments pointing `RECORDING_WORKER_CLASSES` at their own worker
class. The shipped `VideoCompositeEgressService` and `AudioCompositeEgressService`
are already updated.

The `WorkerService` protocol's `start()` takes a third argument, and the mediator
now always passes it as a keyword when the recording carries no per-recording encoding:

```python
# before
def start(self, room_id: str, recording_id: str) -> str: ...

# now
def start(
    self,
    room_id: str,
    recording_id: str,
    encoding_options: Optional[Dict[str, Any]] = None,
) -> str: ...
```

#### Optional: per-recording encoding

`RECORDING_CUSTOM_ENCODING_ENABLED` (default `False`) toggles whether the
start-recording API accepts an `encoding` object
(`{"resolution": "720p", "profile": "talking_heads"}`, `profile` optional. It
falls back to `RECORDING_ENCODING_DEFAULT_PROFILE`) that overrides the default for
a single recording. It does not enable or disable the
default encoding, which is built from the two `RECORDING_ENCODING_DEFAULT_*`
settings either way. Leaving it at `False` preserves the previous behaviour, where
every recording uses the server-side encoding: requests carrying
`options.encoding` are rejected with a `400` before the recording is created, so
nothing is persisted and no egress is started.

Before enabling it:

- clients can only pick keys you declared; there is no way to send a raw width or bitrate
- as of this implementation, the frontend never sends `encoding` 
- `encoding` is accepted but ignored for `transcript` recordings, whose audio-only
  egress has no video encoding to configure.

See [docs/features/recording.md](docs/features/recording.md#tuning-recording-encoding)
for the full setting reference, the shipped profile table and the tuning caveats.

## v1.33.0

### Purging inactive rooms

Rooms now keep track of the last time they were started (`last_started_at`), fed by LiveKit's `room_started` webhook. A new `purge_inactive_rooms` management command permanently deletes the rooms that have not been started for `ROOM_INACTIVITY_DELETION_DAYS` days. See [the room purge documentation](docs/features/room-purge.md).

- The feature is **disabled by default**: nothing is deleted unless you set `ROOM_INACTIVITY_DELETION_DAYS`.
- The migration marks every existing room as started at the time of the upgrade, so no existing room can be purged before a full inactivity period has elapsed after upgrading.
- Rooms holding a saved recording their users may still access are kept: any saved recording, or, when `RECORDING_EXPIRATION_DAYS` is set, a saved recording created within that window.
- Inactivity is measured from LiveKit's `room_started` webhook: if it is not delivered to your backend, rooms in daily use look inactive and get purged.
- When a room is purged, all it's configuration and access rights are also deleted. Its slug becomes available again and can be reused when a meeting is created from that same URL.

* With `ALLOW_UNREGISTERED_ROOMS=false`, only an authenticated user can navigate to a previously existing link after the room has been purged. Doing so recreates the room in the database with a fresh configuration, with that user associated with it and granted admin rights.
* With `ALLOW_UNREGISTERED_ROOMS=true`, any user can reopen the purged room by navigating to the same URL. In that case, the room is created dynamically and no corresponding room entry is persisted in the database.

### Local development: MinIO replaced by Garage

The development stacks now use [Garage](https://garagehq.deuxfleurs.fr/) instead of MinIO as S3 storage. Garage keeps its own format in `data/media/meta` and `data/media/data` and cannot read what MinIO left there, so local recordings and files will be lost.

To migrate a local environment:

1. Stop the stack and remove its containers, including the former `minio` one: `docker compose down --remove-orphans`
2. Optionally reclaim the space used by MinIO: `rm -rf data/media && make data/media`
3. In your `env.d/development/*` files, replace `minio:9000` by `garage:9000`, the `meet` / `password` credentials by `meet-access-key` / `meet-secret-access-key`, and add `AWS_S3_REGION_NAME=local` (or delete these files and run `make create-env-files`)
4. Run `make create-env-files` to generate `env.d/development/garage`, which holds a random RPC secret for Garage.
5. Rebuild the images, since the summary and agent images now install boto3 instead of minio

### Summary service and metadata collector: boto3 replaces the minio client

The summary service and the metadata collector agent now talk to S3 through boto3 instead of the minio client, with the same settings.
Requests are now signed for `AWS_S3_REGION_NAME` as-is. When it is not set, the region is no longer looked up from the bucket: boto3 falls back to `AWS_DEFAULT_REGION`. If you left `AWS_S3_REGION_NAME` unset, set it to your provider's region before upgrading, or providers that check the signing region will reject the transcripts, summaries and meeting metadata uploads, as well as their signed URLs.

Also:
- Signed URLs to transcripts and summaries are now always path-style (`<endpoint>/<bucket>/<key>`), whereas the minio client used virtual-hosted-style URLs
- The metadata collector now accepts `AWS_S3_ENDPOINT_URL` with or without a scheme, like the summary service: the scheme always follows `AWS_S3_SECURE_ACCESS`.

### Helm chart: media services default to Garage

The `meet` chart now defaults `serviceMedia.host` and `serviceMediaFiles.host` to `garage.meet.svc.cluster.local`, and the `upstream-vhost` annotation of `ingressMedia` and `ingressMediaFiles` to `garage.meet.svc.cluster.local:9000`. If you relied on the former `minio.meet.svc.cluster.local` defaults, set these values explicitly to your S3 service before upgrading, or recordings and files stop being served under `/media`.

## v1.30.0

### Removing S3 storage-event webhooks for recordings

Recordings were previously confirmed as saved by an S3 storage-event webhook posting to `/api/v1.0/recordings/storage-hook/`. That endpoint has been removed: recordings are now always finalized from LiveKit's own `egress_ended` webhook, which has been the default path since v1.22.0.

**Required for every deployment:** LiveKit must be able to deliver webhooks to the backend at `/api/v1.0/rooms/webhooks-livekit/`. This is now the only way a recording reaches a saved state; if `egress_ended` is never delivered, recordings stay in the `active` state.

For hosters who had configured storage-event webhooks:
- Recordings reach the same final state, but they are now finalized when LiveKit reports the egress as ended rather than when the storage backend reports the upload.
- Remove the event notification from your bucket configuration: it now targets a non-existent endpoint and will fail on every delivery.

For hosters who had **not** configured storage-event webhooks:
- Nothing changes. Recordings have been finalized from the `egress_ended` webhook since v1.22.0.

In both cases, the following settings are no longer used and can be removed from your env: `RECORDING_EVENT_PARSER_CLASS`, `RECORDING_ENABLE_STORAGE_EVENT_AUTH`, `RECORDING_STORAGE_EVENT_ENABLE`, `RECORDING_STORAGE_EVENT_TOKEN`.

On completion of the egress, a recording moves to `notification_succeeded`, or to `saved` if notifying external services failed.

## v1.23.0

As part of the 1.23.0 release, the legacy `api/v1` implementation has been removed from the _experimental_ Summary service and Meet has been migrated to the new `api/v2`.

**To avoid a breaking change, the Meet backend continues to use the Summary service's v1-compatible API format by default (`SUMMARY_SERVICE_VERSION` setting defaults to `1`).**

If you are deploying both Meet and Summary from this repository, you must configure the Meet backend to use the v2 API by setting the following environment variable `SUMMARY_SERVICE_VERSION=2`.

If you are upgrading only the Meet deployment while keeping an older Summary v1 compatible deployment, no action is required, as the v1-compatible API remains the default.

Note that we plan on removing the legacy `v1` summary compatibility in a future major version. If you have your own implementation for the summary service, we recommend updating its API contract and setting `SUMMARY_SERVICE_VERSION=2`.
