"""Tests of the LinTO catch-up without live transcription (fork)."""

# pylint: disable=W0621,W0212,E1101,W0613,too-many-lines

import json
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.core.cache import cache

import aiohttp
import botocore
import pytest
from livekit import api

from core.factories import RecordingFactory
from core.recording.enums import RecordingWorkerEvent
from core.services import linto_catchup
from core.services.livekit_events import LiveKitEventsService
from core.tasks.linto_catchup import reconcile_linto_catchup

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
ROUTE = "visio-catchup-brief"


@pytest.fixture(autouse=True)
def catchup_settings(settings):
    """Feature on, Studio configured, deterministic durations."""
    settings.LINTO_CATCHUP_ENABLED = True
    settings.LINTO_STUDIO_BASE_URL = "http://studio-api"
    settings.LINTO_CATCHUP_PREFIX = "catchup"
    settings.LINTO_CATCHUP_SEGMENT_DURATION_S = 10
    settings.LINTO_CATCHUP_LATE_THRESHOLD_S = 30
    settings.LINTO_CATCHUP_SUMMARY_SERVICE = ROUTE
    settings.LINTO_CATCHUP_REUSE_S = 120
    settings.LINTO_CATCHUP_MIN_CHARS = 20
    settings.LINTO_CATCHUP_JOB_TIMEOUT_S = 1800
    settings.LINTO_ENTITLEMENTS_ENABLED = False
    settings.LIVEKIT_CONFIGURATION = {
        "api_key": "key",
        "api_secret": "secret",
        "url": "https://livekit.example.com/",
    }
    return settings


def _playlist(start, names, durations=None):
    lines = ["#EXTM3U", "#EXT-X-VERSION:4", "#EXT-X-TARGETDURATION:10"]
    t = start
    for name in names:
        duration = (durations or {}).get(name, 10.0)
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{t.isoformat().replace('+00:00', 'Z')}")
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(name)
        t += timedelta(seconds=duration)
    return "\n".join(lines)


def _egress(prefix, egress_id="EG_catchup", started_at=None):
    info = api.EgressInfo(egress_id=egress_id)
    info.room_composite.segment_outputs.append(
        api.SegmentedFileOutput(filename_prefix=prefix)
    )
    if started_at is not None:
        info.started_at = int(started_at.timestamp() * 1_000_000_000)
    return info


def _segments(count):
    return [
        {
            "name": f"seg_{i:05d}.ts",
            "start": T0 + timedelta(seconds=10 * i),
            "duration_ms": 10000,
            "key": f"catchup/r/seg_{i:05d}.ts",
        }
        for i in range(count)
    ]


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


# ── Pure helpers ────────────────────────────────────────────────────────────


def test_parse_playlist():
    """Names, wall-clock starts and durations are read from the playlist."""
    segments = linto_catchup.parse_playlist(
        _playlist(T0, ["seg_00000.ts", "seg_00001.ts"], {"seg_00001.ts": 4.5})
    )
    assert [s["name"] for s in segments] == ["seg_00000.ts", "seg_00001.ts"]
    assert segments[0]["start"] == T0
    assert segments[1]["start"] == T0 + timedelta(seconds=10)
    assert segments[1]["duration_ms"] == 4500


def test_catchup_prefix_detection():
    """Only an egress writing under the catch-up prefix is a catch-up egress."""
    info = _egress("catchup/abc/seg")
    assert linto_catchup.catchup_segment_prefix(info) == "catchup/abc/seg"
    assert linto_catchup.recording_id_of("catchup/abc/seg") == "abc"
    assert linto_catchup.catchup_segment_prefix(api.EgressInfo(egress_id="x")) is None
    assert linto_catchup.catchup_segment_prefix(_egress("recordings/abc")) is None


@pytest.mark.parametrize(
    ("mode", "options", "eligible"),
    (
        ("transcript", {"catchup": True}, True),
        ("transcript", {}, True),
        ("transcript", {"catchup": False}, True),
        ("screen_recording", {"transcribe": True, "catchup": True}, True),
        ("screen_recording", {"original_mode": "transcript", "catchup": True}, True),
        ("screen_recording", {"catchup": True}, False),
    ),
)
def test_is_eligible(mode, options, eligible):
    """Every transcribed recording: the brief has no option any more."""
    recording = RecordingFactory(mode=mode, options=options)
    assert linto_catchup.is_eligible(recording) is eligible


def test_list_segments_merges_playlists_and_listing():
    """The main playlist lags; the live one and the bucket listing fill in."""
    main = _playlist(T0, ["seg_00000.ts", "seg_00001.ts"])
    live = _playlist(T0 + timedelta(seconds=20), ["seg_00002.ts"])
    objects = {"catchup/r1/index.m3u8": main, "catchup/r1/live.m3u8": live}
    listing = ["seg_00000.ts", "seg_00001.ts", "seg_00002.ts", "seg_00003.ts"]
    with (
        mock.patch.object(linto_catchup, "_read_object", side_effect=objects.get),
        mock.patch.object(linto_catchup, "_list_segment_objects", return_value=listing),
    ):
        segments = linto_catchup.list_segments("r1")

    assert [s["name"] for s in segments] == listing
    assert segments[3]["start"] == T0 + timedelta(seconds=30)  # inferred
    assert segments[3]["key"] == "catchup/r1/seg_00003.ts"


def test_segments_between_keeps_what_is_not_sent_yet():
    """Midpoint after what Meet holds, and not after the arrival."""
    with mock.patch.object(linto_catchup, "list_segments", return_value=_segments(10)):
        first = linto_catchup.segments_between("r", None, T0 + timedelta(seconds=65))
        delta = linto_catchup.segments_between(
            "r", T0 + timedelta(seconds=40), T0 + timedelta(seconds=95)
        )
    assert [s["name"] for s in first] == [f"seg_{i:05d}.ts" for i in range(6)]
    assert [s["name"] for s in delta] == [f"seg_{i:05d}.ts" for i in range(4, 9)]


def test_segments_between_without_end_takes_the_rest():
    """At the end of the recording, every segment not sent yet."""
    with mock.patch.object(linto_catchup, "list_segments", return_value=_segments(10)):
        rest = linto_catchup.segments_between("r", T0 + timedelta(seconds=40), None)
    assert [s["name"] for s in rest] == [f"seg_{i:05d}.ts" for i in range(4, 10)]


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg",
)
def test_studio_audio_remuxes_mpeg_ts_into_m4a(tmp_path):
    """Segments cut mid-stream become an M4A starting at 0, same audio, lighter."""
    ts_path, m4a_path = tmp_path / "delta.ts", tmp_path / "delta.m4a"

    def run(command, *paths):
        return subprocess.run(
            [*command.split(), *paths], check=True, capture_output=True
        ).stdout

    def pcm(path):
        return run("ffmpeg -v error -i", path, "-f", "s16le", "-")

    run(
        "ffmpeg -v error -f lavfi -i sine=f=440:d=5 "
        "-c:a aac -output_ts_offset 3660 -f mpegts",
        ts_path,
    )
    m4a_path.write_bytes(linto_catchup.studio_audio(ts_path.read_bytes()))

    probe = json.loads(
        run(
            "ffprobe -v error -show_entries format=format_name,start_time -of json",
            m4a_path,
        )
    )["format"]
    assert "mp4" in probe["format_name"]
    assert float(probe["start_time"]) == 0
    assert pcm(m4a_path) == pcm(ts_path)
    assert m4a_path.stat().st_size < ts_path.stat().st_size


@pytest.mark.parametrize(
    "error",
    (subprocess.CalledProcessError(1, "ffmpeg"), OSError(28, "No space left")),
)
def test_studio_audio_falls_back_to_mpeg_ts(error):
    """When ffmpeg or its temp files fail, the MPEG-TS goes as it is (Studio reads it)."""
    with mock.patch.object(
        linto_catchup, "extract_audio_from_video", side_effect=error
    ):
        assert linto_catchup.studio_audio(b"TS") == b"TS"


def test_read_segments_concatenates_in_order():
    """MPEG-TS segments of one stream are concatenated in their order."""
    data = {"a": b"A" * 3, "b": b"B", "c": b"CC"}
    with mock.patch.object(linto_catchup, "_read_bytes", side_effect=data.get):
        assert linto_catchup.read_segments(["a", "b", "c"]) == b"AAABCC"
    assert linto_catchup.read_segments([]) == b""


@pytest.mark.parametrize(
    ("mode", "studio", "segments"),
    (
        ("transcript", True, True),
        ("transcript", False, False),
        ("screen_recording", True, False),
    ),
)
def test_an_audio_recording_is_recorded_as_segments(settings, mode, studio, segments):
    """With the catch-up on, an audio recording's one egress writes segments."""
    settings.LINTO_STUDIO_ENABLED = studio
    assert linto_catchup.records_segments(mode) is segments


def test_records_nothing_as_segments_when_the_catchup_is_off(settings):
    """The feature off: the audio recording keeps its file egress."""
    settings.LINTO_STUDIO_ENABLED = True
    settings.LINTO_CATCHUP_ENABLED = False
    assert linto_catchup.records_segments("transcript") is False


def test_a_segmented_recording_is_marked_on_the_recording():
    """The mode is kept with the recording."""
    assert linto_catchup.is_segmented(RecordingFactory(options={"segments": True}))
    assert not linto_catchup.is_segmented(RecordingFactory(options={}))


def test_the_segments_egress_writes_under_the_recording_prefix(settings):
    """Audio only, 10 s segments and both playlists under catchup/<id>/."""
    service = linto_catchup.SegmentsEgressService(
        linto_catchup.WorkerServiceConfig.from_settings()
    )
    with mock.patch.object(
        service,
        "_handle_request",
        return_value=api.EgressInfo(egress_id="EG_rec"),
    ) as handle:
        assert service.start("room", "rec-1") == "EG_rec"
    request, method = handle.call_args.args
    assert method == "start_room_composite_egress"
    assert request.audio_only is True
    output = request.segment_outputs[0]
    assert output.filename_prefix == "catchup/rec-1/seg"
    assert output.playlist_name == "catchup/rec-1/index.m3u8"
    assert output.live_playlist_name == "catchup/rec-1/live.m3u8"
    assert output.segment_duration == 10


def test_a_recording_own_segments_egress_is_not_a_side_capture():
    """A segmented recording's egress is not a side capture."""
    segmented = RecordingFactory(mode="transcript", options={"segments": True})
    video = RecordingFactory(mode="screen_recording", options={"transcribe": True})
    assert not linto_catchup.is_side_capture(_egress(f"catchup/{segmented.id}/seg"))
    assert linto_catchup.is_side_capture(_egress(f"catchup/{video.id}/seg"))
    assert linto_catchup.is_side_capture(_egress("catchup/not-a-uuid/seg"))
    assert not linto_catchup.is_side_capture(_egress("recordings/abc"))


def _playlist_object(text, modified=T0):
    return {
        "Body": mock.Mock(read=mock.Mock(return_value=text.encode())),
        "LastModified": modified,
    }


def test_segments_closed_once_the_playlist_ends():
    """The playlist's end marker says the egress closed the segments."""
    s3 = mock.MagicMock()
    with mock.patch.object(linto_catchup, "_s3", return_value=s3):
        s3.get_object.return_value = _playlist_object(_playlist(T0, ["seg_00000.ts"]))
        assert linto_catchup.segments_closed_at("r1") is None
        s3.get_object.return_value = _playlist_object(
            _playlist(T0, ["seg_00000.ts"]) + "\n#EXT-X-ENDLIST\n"
        )
        assert linto_catchup.segments_closed_at("r1") == T0
    assert s3.get_object.call_args.kwargs["Key"] == "catchup/r1/index.m3u8"


def test_live_run_active():
    """Only a live run of its own that offers the catch-up serves late joiners."""
    room = RecordingFactory().room
    assert linto_catchup.live_run_active(room) is False
    room.configuration = {"linto": {"session_id": "s1"}}
    assert linto_catchup.live_run_active(room) is True
    room.configuration = {"linto": {"session_id": "s1", "captions_only": True}}
    assert linto_catchup.live_run_active(room) is False
    room.configuration = {"linto": {"session_id": "s1", "catchup": False}}
    assert linto_catchup.live_run_active(room) is False


# ── Late joiner: request_brief ─────────────────────────────────────────────


@pytest.fixture
def active_transcript():
    """An active audio recording written as segments."""
    return RecordingFactory(
        mode="transcript",
        status="active",
        options={"language": "fr", "catchup": True, "segments": True},
    )


@pytest.fixture
def active_video():
    """An active transcribed video recording with the catch-up option on."""
    return RecordingFactory(
        mode="screen_recording",
        status="active",
        options={"language": "fr", "catchup": True, "transcribe": True},
    )


@pytest.fixture(autouse=True)
def no_summary_breaker():
    """No "summary service unavailable" left by another test."""
    cache.delete(linto_catchup.SUMMARY_UNAVAILABLE_KEY)
    yield
    cache.delete(linto_catchup.SUMMARY_UNAVAILABLE_KEY)


@pytest.fixture
def studio():
    """A fake Studio client."""
    client = mock.MagicMock()
    client.create.return_value = "conv-1"
    with mock.patch.object(linto_catchup, "StudioClient", return_value=client):
        yield client


@pytest.fixture
def no_speaker_identification():
    """The organization's voiceprint collection is not looked up."""
    with mock.patch(
        "core.services.linto_catchup.resolve_speaker_collection_ids",
        new=mock.AsyncMock(return_value=["col-1"]),
    ) as resolve:
        yield resolve


def _hold(recording, covered_until, conversation_id="conv-1"):
    recording.linto_state = {
        "catchup": {
            "conversation_id": conversation_id,
            "covered_until": _iso(covered_until),
        }
    }
    recording.save()


def _held(recording):
    recording.refresh_from_db()
    return linto_catchup.held_conversation(recording)


@contextmanager
def _segments_in_bucket(segments=12):
    """The bucket's segments, read as their keys, sent without remux."""
    with (
        mock.patch.object(
            linto_catchup, "list_segments", return_value=_segments(segments)
        ),
        mock.patch.object(
            linto_catchup,
            "read_segments",
            side_effect=lambda keys: b"".join(k.encode() for k in keys),
        ) as read,
        mock.patch.object(
            linto_catchup,
            "extract_audio_from_video",
            side_effect=lambda audio, **_: audio,
        ),
    ):
        yield read


@contextmanager
def _stored(recording, joined_at, segments=12):
    """The recording's capture, segments and the caller's arrival, as LiveKit and S3 tell."""
    capture = _egress(f"catchup/{recording.id}/seg", started_at=T0)
    with (
        mock.patch.object(linto_catchup, "find_capture", return_value=capture),
        mock.patch.object(
            linto_catchup,
            "_participant_joined_at_ms",
            return_value=int(joined_at.timestamp() * 1000),
        ),
        _segments_in_bucket(segments) as read,
    ):
        yield read


def _arrive(recording, joined_at, segments=12, **kwargs):
    with _stored(recording, joined_at, segments) as read:
        body = linto_catchup.request_brief(recording.room, "alice", **kwargs)
    return body, read


def _arrive_and_send(recording, joined_at, segments=12):
    """An arrival, then the poll that sends its audio."""
    with _stored(recording, joined_at, segments) as read:
        linto_catchup.request_brief(recording.room, "alice")
        body = linto_catchup.request_status(recording.room, str(recording.id))
    return body, read


def test_an_arrival_answers_before_any_audio_is_sent(active_transcript, studio):
    """The arrival only plans the sending: the browser shows the wait at once."""
    body, read = _arrive(active_transcript, T0 + timedelta(seconds=65))

    assert body["state"] == "sending"
    assert body["progress"] < 0.05
    assert body["eta_s"][0] <= body["eta_s"][1]
    assert body["overdue"] is False
    read.assert_not_called()
    studio.create.assert_not_called()
    job = linto_catchup.load_state(active_transcript.id)["job"]
    assert job["up_to"] == _iso(T0 + timedelta(seconds=60))
    assert job["audio_s"] == 60


def test_first_late_joiner_creates_the_meeting_conversation(
    active_transcript, studio, no_speaker_identification
):
    """The first late joiner creates the conversation with the transcript's settings."""
    body, read = _arrive_and_send(active_transcript, T0 + timedelta(seconds=65))

    assert body["state"] == "transcribing"
    assert body["recording_id"] == str(active_transcript.id)
    keys = read.call_args.args[0]
    assert keys == [f"catchup/r/seg_{i:05d}.ts" for i in range(6)]
    audio = studio.create.call_args.args[0]
    assert audio == b"".join(k.encode() for k in keys)
    assert studio.create.call_args.kwargs == {
        "name": linto_catchup.conversation_name(active_transcript),
        "language": "fr",
        "enable_diarization": True,
        "members_right": 0,
        "speaker_collection_ids": ["col-1"],
    }
    studio.append.assert_not_called()
    job = linto_catchup.load_state(active_transcript.id)["job"]
    assert job["conversation_id"] == "conv-1"
    assert job["up_to"] == _iso(T0 + timedelta(seconds=60))
    assert job["audio_s"] == 60
    assert _held(active_transcript) == {
        "conversation_id": "conv-1",
        "covered_until": _iso(T0 + timedelta(seconds=60)),
    }


def test_the_late_joiners_audio_goes_remuxed(
    active_transcript, studio, no_speaker_identification
):
    """Studio gets the segments remuxed into M4A, not their MPEG-TS."""
    with (
        _stored(active_transcript, T0 + timedelta(seconds=65)) as read,
        mock.patch.object(
            linto_catchup, "extract_audio_from_video", return_value=b"M4A"
        ) as remux,
    ):
        linto_catchup.request_brief(active_transcript.room, "alice")
        linto_catchup.request_status(active_transcript.room, str(active_transcript.id))
    keys = read.call_args.args[0]
    remux.assert_called_once_with(
        b"".join(k.encode() for k in keys), output_format="copy", input_suffix=".ts"
    )
    assert studio.create.call_args.args[0] == b"M4A"


def test_next_to_a_video_the_conversation_only_serves_the_briefs(active_video, studio):
    """Next to a video, the conversation has no diarization."""
    _arrive_and_send(active_video, T0 + timedelta(seconds=65))
    assert studio.create.call_args.kwargs == {
        "name": f"Visio catch-up {active_video.id}",
        "language": "fr",
        "enable_diarization": False,
        "members_right": 0,
    }


def test_a_later_arrival_appends_only_the_delta(active_transcript, studio):
    """Only the minutes after the audio sent go, to the same conversation."""
    _hold(active_transcript, T0 + timedelta(seconds=40))
    linto_catchup.save_state(
        active_transcript.id,
        {
            "brief": {"text": "Old", "covered_until": _iso(T0 + timedelta(seconds=40))},
            "job": None,
            "outcome": "ready",
        },
    )
    _, read = _arrive_and_send(
        active_transcript, T0 + timedelta(seconds=300), segments=12
    )

    keys = [f"catchup/r/seg_{i:05d}.ts" for i in range(4, 12)]
    assert read.call_args.args[0] == keys
    studio.create.assert_not_called()
    studio.append.assert_called_once_with("conv-1", b"".join(k.encode() for k in keys))
    assert _held(active_transcript)["covered_until"] == _iso(
        T0 + timedelta(seconds=120)
    )


def test_not_late_gets_nothing(active_transcript, studio):
    """A participant present from the start gets nothing; Studio is not called."""
    body, _ = _arrive(active_transcript, T0 + timedelta(seconds=5))
    assert body["state"] == "none"
    studio.create.assert_not_called()


def test_client_can_only_ask_for_less(
    active_transcript, studio, no_speaker_identification
):
    """After a reload, the earlier client join time wins (min)."""
    client_ms = int((T0 + timedelta(seconds=65)).timestamp() * 1000)
    _arrive(
        active_transcript,
        T0 + timedelta(seconds=300),
        client_joined_at_ms=client_ms,
    )
    job = linto_catchup.load_state(active_transcript.id)["job"]
    assert job["up_to"] == _iso(T0 + timedelta(seconds=60))


def test_an_arrival_attaches_to_the_running_job(
    active_transcript, studio, no_speaker_identification
):
    """While a job runs, a new arrival waits for it: nothing more is sent."""
    _arrive_and_send(active_transcript, T0 + timedelta(seconds=65))
    body, _ = _arrive(active_transcript, T0 + timedelta(seconds=95))
    assert body["state"] == "transcribing"
    assert studio.create.call_count == 1
    studio.append.assert_not_called()


def test_a_recent_brief_is_reused(active_transcript, studio):
    """A brief covering up to two minutes before the arrival answers at once."""
    _hold(active_transcript, T0 + timedelta(seconds=60))
    linto_catchup.save_state(
        active_transcript.id,
        {
            "brief": {
                "text": "Brief",
                "covered_until": _iso(T0 + timedelta(seconds=60)),
            },
            "job": None,
            "outcome": "ready",
        },
    )
    body, _ = _arrive(active_transcript, T0 + timedelta(seconds=150))
    assert body["state"] == "ready"
    assert body["brief"]["text"] == "Brief"
    studio.append.assert_not_called()


def test_another_arrival_sending_says_sending(active_transcript, studio):
    """Someone else is sending audio: answer "sending", start nothing."""
    cache.add(linto_catchup._lock_key(active_transcript.id, "start"), 1, 60)
    try:
        body, _ = _arrive(active_transcript, T0 + timedelta(seconds=65))
    finally:
        cache.delete(linto_catchup._lock_key(active_transcript.id, "start"))
    assert body["state"] == "sending"
    studio.create.assert_not_called()


def test_nothing_recorded_yet_is_too_short(active_transcript, studio):
    """No segment before the arrival and no brief: too short."""
    body, _ = _arrive(active_transcript, T0 + timedelta(seconds=65), segments=0)
    assert body["state"] == "too_short"
    studio.create.assert_not_called()


def test_without_capture_the_catchup_is_unavailable(active_transcript, studio):
    """No egress writing segments: unavailable, Studio is not called."""
    with mock.patch.object(linto_catchup, "find_capture", return_value=None):
        body = linto_catchup.request_brief(active_transcript.room, "alice")
    assert body["state"] == "unavailable"
    studio.create.assert_not_called()


def test_without_recording_there_is_nothing():
    """No transcribed recording: nothing to offer."""
    recording = RecordingFactory(mode="screen_recording", status="active")
    assert linto_catchup.request_brief(recording.room, "alice")["state"] == "none"


def test_a_live_run_of_its_own_serves_instead(active_transcript, studio):
    """A live run with its catch-up serves the late joiners itself."""
    room = active_transcript.room
    room.configuration = {"linto": {"session_id": "s1", "org_id": "o1"}}
    room.save()
    assert linto_catchup.request_brief(room, "alice") == {"state": "live"}
    studio.create.assert_not_called()


def test_live_captions_leave_the_brief_to_the_recording(
    active_transcript, studio, no_speaker_identification
):
    """Live captions on top of the recording keep nothing: the audio brief runs."""
    room = active_transcript.room
    room.configuration = {"linto": {"session_id": "s1", "captions_only": True}}
    room.save()
    active_transcript.refresh_from_db()
    body, _ = _arrive(active_transcript, T0 + timedelta(seconds=65))
    assert body["state"] == "sending"


def test_the_owner_needs_the_summary_capability(active_transcript, settings):
    """No summary right for the owner: no brief."""
    with mock.patch.object(linto_catchup, "summary_entitled", return_value=False):
        assert linto_catchup.is_eligible(active_transcript) is False
        assert linto_catchup.request_brief(active_transcript.room, "alice") == {
            "state": "none"
        }


def test_a_participant_not_in_the_room_is_refused(active_transcript):
    """Someone not in the room is refused."""
    capture = _egress(f"catchup/{active_transcript.id}/seg", started_at=T0)
    with (
        mock.patch.object(linto_catchup, "find_capture", return_value=capture),
        mock.patch.object(
            linto_catchup, "_participant_joined_at_ms", return_value=None
        ),
        pytest.raises(linto_catchup.NotInRoomError),
    ):
        linto_catchup.request_brief(active_transcript.room, "mallory")


# ── The job, one step per poll: request_status ─────────────────────────────


def _running(recording, step="transcribing", brief=None, **job):
    linto_catchup.save_state(
        recording.id,
        {
            "brief": brief,
            "job": {
                "conversation_id": "conv-1",
                "from": _iso(T0),
                "up_to": _iso(T0 + timedelta(seconds=60)),
                "step": step,
                "started_at": linto_catchup._iso(linto_catchup._now()),
                "step_started_at": linto_catchup._iso(linto_catchup._now()),
                "audio_s": 60,
                "errors": 0,
                **job,
            },
            "outcome": None,
        },
    )


def _parts(*states):
    return [{"id": f"p{i}", "state": state} for i, state in enumerate(states)]


def _conversation(*segments):
    return {
        "text": [{"turn_id": f"t{i}", "segment": s} for i, s in enumerate(segments)]
    }


LONG_ENOUGH = "La suite de la réunion, assez longue pour un résumé."


def _status(recording):
    return linto_catchup.request_status(recording.room, str(recording.id))


def test_a_part_being_transcribed_is_reported(active_transcript, studio):
    """While Studio transcribes a part, the state says so with an estimate."""
    _running(active_transcript)
    studio.parts.return_value = _parts("done", "pending")
    body = _status(active_transcript)
    assert body["state"] == "transcribing"
    assert 0 < body["progress"] < 1
    assert body["eta_s"][0] <= body["eta_s"][1]
    studio.trigger_summary.assert_not_called()


def test_every_part_in_asks_for_a_new_summary(active_transcript, studio):
    """Once every part is merged, the whole conversation is summarized."""
    _running(active_transcript)
    studio.parts.return_value = _parts("done", "done")
    studio.conversation.return_value = _conversation("Bonjour à tous.", LONG_ENOUGH)
    body = _status(active_transcript)

    assert body["state"] == "summarizing"
    studio.trigger_summary.assert_called_once_with("conv-1", ROUTE)
    studio.delete.assert_not_called()


def test_a_failed_part_still_leaves_the_rest_to_summarize(
    active_transcript, studio, caplog
):
    """A part Studio failed to transcribe: a warning, the rest is summarized."""
    _running(active_transcript)
    studio.parts.return_value = _parts("done", "error")
    studio.conversation.return_value = _conversation(LONG_ENOUGH)
    with caplog.at_level("WARNING", logger="core.services.linto_catchup"):
        assert _status(active_transcript)["state"] == "summarizing"
    assert any("last part" in r.getMessage() for r in caplog.records)


def test_too_little_said_is_too_short(active_transcript, studio):
    """Nothing worth summarizing: too short, the conversation stays."""
    _running(active_transcript)
    studio.parts.return_value = _parts("done")
    studio.conversation.return_value = _conversation("Oui.")
    body = _status(active_transcript)
    assert body["state"] == "too_short"
    studio.trigger_summary.assert_not_called()
    studio.delete.assert_not_called()


def test_the_summary_becomes_the_brief(active_transcript, studio):
    """Once summarized, the brief is kept; the conversation stays."""
    _running(active_transcript, step="summarizing")
    studio.summary.return_value = ("done", "### En cours\nLe budget.")
    body = _status(active_transcript)

    assert body["state"] == "ready"
    assert body["brief"] == {
        "text": "### En cours\nLe budget.",
        "covered_until": _iso(T0 + timedelta(seconds=60)),
    }
    studio.delete.assert_not_called()
    assert linto_catchup.load_state(active_transcript.id)["job"] is None


def test_a_running_summary_is_reported(active_transcript, studio):
    """The summary runs: summarizing."""
    _running(active_transcript, step="summarizing")
    studio.summary.return_value = ("running", None)
    assert _status(active_transcript)["state"] == "summarizing"


def test_a_failed_summary_keeps_the_previous_brief(active_transcript, studio):
    """The summary failed: the previous brief still serves."""
    brief = {"text": "Old", "covered_until": _iso(T0)}
    _running(active_transcript, step="summarizing", brief=brief)
    studio.summary.return_value = ("error", None)
    body = _status(active_transcript)
    assert body["state"] == "ready"
    assert body["brief"]["text"] == "Old"


def test_repeated_studio_errors_fail_the_job(active_transcript, studio):
    """Studio keeps failing: the job is given up after a few polls."""
    _running(active_transcript)
    studio.parts.side_effect = linto_catchup.CatchupError("down")
    for _ in range(linto_catchup.MAX_STEP_ERRORS - 1):
        assert _status(active_transcript)["state"] == "transcribing"
    assert _status(active_transcript)["state"] == "failed"


def test_a_job_past_its_timeout_fails(active_transcript, studio, settings):
    """A job running too long is given up; the conversation stays."""
    settings.LINTO_CATCHUP_JOB_TIMEOUT_S = 1
    _running(active_transcript, started_at=_iso(T0))
    assert _status(active_transcript)["state"] == "failed"
    studio.delete.assert_not_called()


def test_one_poll_advances_at_a_time(active_transcript, studio):
    """Another poll is advancing the job: answer without a Studio call."""
    _running(active_transcript)
    key = linto_catchup._lock_key(active_transcript.id, "step")
    cache.add(key, 1, 60)
    try:
        assert _status(active_transcript)["state"] == "transcribing"
    finally:
        cache.delete(key)
    studio.parts.assert_not_called()


def test_status_of_another_room_recording_is_none(active_transcript):
    """A recording of another room is never read."""
    other = RecordingFactory(mode="transcript", status="active")
    assert linto_catchup.request_status(other.room, str(active_transcript.id)) == {
        "state": "none"
    }


def _sending(recording):
    _running(recording, step="sending", conversation_id=None)


def _poll_with_segments(recording, segments=12):
    with _stored(recording, T0 + timedelta(seconds=65), segments):
        return _status(recording)


def test_the_sending_waits_for_another_one(active_transcript, studio):
    """Audio already being sent (another poll, the end of the recording): wait."""
    _sending(active_transcript)
    key = linto_catchup._lock_key(active_transcript.id, "start")
    cache.add(key, 1, 60)
    try:
        assert _poll_with_segments(active_transcript)["state"] == "sending"
    finally:
        cache.delete(key)
    studio.create.assert_not_called()


def test_a_sending_refused_by_studio_fails_at_once(
    active_transcript, studio, no_speaker_identification, caplog
):
    """Studio refuses the audio: sending it again would not help."""
    _sending(active_transcript)
    studio.create.side_effect = linto_catchup.StudioHTTPError(
        "Studio: HTTP 400", 400, ""
    )
    with caplog.at_level("WARNING", logger="core.services.linto_catchup"):
        assert _poll_with_segments(active_transcript)["state"] == "failed"
    assert any("refused by Studio" in r.getMessage() for r in caplog.records)
    assert studio.create.call_count == 1


def test_a_sending_failing_on_studio_side_is_tried_again(
    active_transcript, studio, no_speaker_identification
):
    """Studio down while sending: the next poll sends again."""
    _sending(active_transcript)
    studio.create.side_effect = [
        linto_catchup.StudioHTTPError("Studio: HTTP 503", 503, ""),
        "conv-1",
    ]
    assert _poll_with_segments(active_transcript)["state"] == "sending"
    assert _poll_with_segments(active_transcript)["state"] == "transcribing"


def test_the_end_of_the_recording_may_have_sent_the_minutes(active_transcript, studio):
    """The hand-over sent everything: the job goes on with the finished conversation."""
    _sending(active_transcript)
    _hold(active_transcript, T0 + timedelta(seconds=120))
    body = _poll_with_segments(active_transcript)
    assert body["state"] == "transcribing"
    assert body["up_to"] == _iso(T0 + timedelta(seconds=120))
    studio.create.assert_not_called()
    studio.append.assert_not_called()


def test_no_conversation_is_created_once_the_recording_ended(active_transcript, studio):
    """The recording ended before the first sending: its audio is uploaded whole."""
    _sending(active_transcript)
    active_transcript.status = "stopped"
    active_transcript.save()
    assert _poll_with_segments(active_transcript)["state"] == "failed"
    studio.create.assert_not_called()


def test_a_step_running_late_has_no_estimate(active_transcript, studio):
    """Past the step's longest estimate, the time left is not made up."""
    _running(active_transcript, step_started_at=_iso(T0))
    studio.parts.return_value = _parts("pending")
    body = _status(active_transcript)
    assert body["state"] == "transcribing"
    assert body["eta_s"] is None
    assert body["overdue"] is True
    assert body["progress"] == pytest.approx(
        linto_catchup.STEP_PROGRESS["transcribing"]
    )


def test_the_previous_brief_shows_while_the_next_is_prepared(active_transcript, studio):
    """A new job keeps the last brief in view."""
    brief = {"text": "Old", "covered_until": _iso(T0)}
    _running(active_transcript, brief=brief)
    studio.parts.return_value = _parts("pending")
    body = _status(active_transcript)
    assert body["state"] == "transcribing"
    assert body["brief"] == {"text": "Old", "covered_until": _iso(T0)}


# ── The summary service missing or failing ─────────────────────────────────


def _every_part_in(studio):
    studio.parts.return_value = _parts("done")
    studio.conversation.return_value = _conversation(LONG_ENOUGH)


def test_a_missing_summary_service_ends_the_job_at_once_with_a_warning(
    active_transcript, studio, caplog
):
    """A missing summary service ends the job at once with a warning."""
    _running(active_transcript)
    _every_part_in(studio)
    studio.trigger_summary.side_effect = linto_catchup.StudioHTTPError(
        "Studio POST /conversations/conv-1/download: HTTP 502",
        502,
        "Request failed with status code 404",
    )
    with caplog.at_level("WARNING", logger="core.services.linto_catchup"):
        body = _status(active_transcript)

    assert body["state"] == "failed"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        ROUTE in m and "Request failed with status code 404" in m and "UPGRADE.md" in m
        for m in warnings
    ), warnings
    assert ROUTE in linto_catchup.summary_unavailable()


def test_after_a_summary_failure_new_arrivals_send_nothing(active_transcript, studio):
    """While the summary service is known to fail, a late joiner gets
    "unavailable" at once: no audio is sent nor transcribed."""
    cache.set(linto_catchup.SUMMARY_UNAVAILABLE_KEY, f"{ROUTE}: HTTP 502", 300)
    body, read = _arrive(active_transcript, T0 + timedelta(seconds=65))
    assert body == {
        "state": "unavailable",
        "reason": "summary_service",
        "recording_id": str(active_transcript.id),
    }
    read.assert_not_called()
    studio.create.assert_not_called()


def test_a_ready_brief_is_still_served_after_a_summary_failure(
    active_transcript, studio
):
    """A brief already written still serves while the service fails."""
    cache.set(linto_catchup.SUMMARY_UNAVAILABLE_KEY, f"{ROUTE}: HTTP 502", 300)
    linto_catchup.save_state(
        active_transcript.id,
        {
            "brief": {
                "text": "Brief",
                "covered_until": _iso(T0 + timedelta(seconds=60)),
            },
            "job": None,
            "outcome": "ready",
        },
    )
    body, _ = _arrive(active_transcript, T0 + timedelta(seconds=150))
    assert body["state"] == "ready"
    assert body["brief"]["text"] == "Brief"


def test_a_replayed_step_asks_for_the_summary_again(active_transcript, studio):
    """Studio unreachable when asked for the summary: the next poll asks again."""
    _running(active_transcript)
    _every_part_in(studio)
    studio.trigger_summary.side_effect = [
        linto_catchup.CatchupError("Studio unreachable: timeout"),
        None,
    ]
    assert _status(active_transcript)["state"] == "transcribing"
    assert _status(active_transcript)["state"] == "summarizing"
    assert studio.trigger_summary.call_count == 2


def test_a_failed_summary_job_marks_the_service_unavailable(
    active_transcript, studio, caplog
):
    """The gateway job itself failed: a warning with its error, and the
    service is marked unavailable for a while."""
    _running(active_transcript, step="summarizing")
    studio.summary.return_value = ("error", "model not found")
    with caplog.at_level("WARNING", logger="core.services.linto_catchup"):
        assert _status(active_transcript)["state"] == "failed"
    assert any("model not found" in r.getMessage() for r in caplog.records)
    assert linto_catchup.summary_unavailable()


def test_a_studio_error_carries_its_detail():
    """Studio's error message is kept in the exception (for the warning)."""
    with (
        mock.patch.object(linto_catchup.StudioClient, "_token", return_value="tok"),
        mock.patch.object(
            linto_catchup.requests,
            "request",
            return_value=_response(
                502, {"status": "error", "error": "Service not found: x"}
            ),
        ),
        pytest.raises(linto_catchup.StudioHTTPError) as raised,
    ):
        linto_catchup.StudioClient().trigger_summary("c1", ROUTE)
    assert raised.value.status == 502
    assert raised.value.detail == "Service not found: x"
    assert "Service not found: x" in str(raised.value)


# ── Studio client ───────────────────────────────────────────────────────────


def _response(status=200, body=None):
    response = mock.MagicMock(status_code=status)
    response.json.return_value = body
    return response


@pytest.fixture
def http():
    """requests.request, with a fixed credential."""
    with (
        mock.patch.object(linto_catchup.StudioClient, "_token", return_value="tok"),
        mock.patch.object(linto_catchup.requests, "request") as request,
    ):
        yield request


@pytest.fixture
def sdk():
    """The ``linto`` SDK, with a fixed credential."""
    linto = mock.MagicMock()
    linto.upload = mock.AsyncMock(return_value="conv-1")
    linto.append = mock.AsyncMock(return_value={})
    linto.complete = mock.AsyncMock(return_value={})
    with (
        mock.patch.object(linto_catchup.StudioClient, "_token", return_value="tok"),
        mock.patch("linto.LinTO", return_value=linto) as linto_class,
    ):
        linto.linto_class = linto_class
        yield linto


def _http_error(status):
    return aiohttp.ClientResponseError(
        request_info=mock.MagicMock(), history=(), status=status, message="refused"
    )


def test_create_opens_the_conversation_to_appended_audio(sdk):
    """The SDK uploads the first minutes with partial."""
    created = linto_catchup.StudioClient().create(
        b"TS", name="Reunion", language="fr", enable_diarization=True
    )
    assert created == "conv-1"
    assert sdk.upload.call_args.kwargs == {
        "file": b"TS",
        "number_of_speaker": "0",
        "enablePunctuation": True,
        "partial": True,
        "name": "Reunion",
        "language": "fr",
        "enable_diarization": True,
    }
    assert sdk.linto_class.call_args.kwargs == {
        "auth_token": "tok",
        "base_url": "http://studio-api",
    }


def test_append_and_complete_go_through_the_sdk(sdk):
    """The audio is appended, then the conversation closed."""
    client = linto_catchup.StudioClient()
    client.append("conv-1", b"TS")
    client.complete("conv-1")
    sdk.append.assert_awaited_once_with("conv-1", b"TS")
    sdk.complete.assert_awaited_once_with("conv-1")


def test_an_sdk_refusal_is_a_studio_http_error(sdk):
    """Studio's status is kept: a 4xx falls back, a 5xx is retried."""
    sdk.append.side_effect = _http_error(404)
    with pytest.raises(linto_catchup.StudioHTTPError) as raised:
        linto_catchup.StudioClient().append("conv-1", b"TS")
    assert raised.value.status == 404


def test_an_sdk_credential_rejected_is_renewed_once(sdk):
    """A 401 renews the credential and retries once."""
    sdk.complete.side_effect = [_http_error(401), {}]
    with mock.patch.object(
        linto_catchup.StudioClient, "_token", side_effect=["old", "new"]
    ) as token:
        linto_catchup.StudioClient().complete("conv-1")
    assert token.call_args_list == [mock.call(False), mock.call(True)]
    assert sdk.complete.await_count == 2


def test_studio_unreachable_through_the_sdk_is_a_catchup_error(sdk):
    """A network error becomes a CatchupError."""
    sdk.upload.side_effect = aiohttp.ClientConnectionError("down")
    with pytest.raises(linto_catchup.CatchupError) as raised:
        linto_catchup.StudioClient().create(b"TS", name="n", language="fr")
    assert not isinstance(raised.value, linto_catchup.StudioHTTPError)


def test_parts_are_read_from_the_job(http):
    """Only the jobs are read to follow the parts."""
    http.return_value = _response(
        200, {"jobs": {"transcription": {"parts": _parts("done", "pending")}}}
    )
    assert [p["state"] for p in linto_catchup.StudioClient().parts("c1")] == [
        "done",
        "pending",
    ]
    assert http.call_args.kwargs["params"] == {"key": "jobs"}


def test_summary_reads_the_export_of_the_service(http):
    """The export of the catch-up service is read once done."""
    http.side_effect = [
        _response(
            200,
            [
                {"format": "other", "status": "done", "jobId": "j0"},
                {"format": ROUTE, "status": "done", "jobId": "j1"},
            ],
        ),
        _response(200, {"status": "success", "content": " Brief "}),
    ]
    assert linto_catchup.StudioClient().summary("c1", ROUTE) == ("done", "Brief")
    assert (
        http.call_args_list[1].args[1].endswith("/conversations/c1/export/j1/content")
    )


@pytest.mark.parametrize(
    ("exports", "status"),
    (
        ([], "running"),
        ([{"format": ROUTE, "status": "processing"}], "running"),
        ([{"format": ROUTE, "status": "error"}], "error"),
    ),
)
def test_summary_states(http, exports, status):
    """No export yet or still running: running; an error: error."""
    http.return_value = _response(200, exports)
    assert linto_catchup.StudioClient().summary("c1", ROUTE)[0] == status


def test_trigger_summary_asks_for_a_new_one(http):
    """Each brief is a new summary of the conversation by the catch-up service."""
    http.return_value = _response(200, {})
    linto_catchup.StudioClient().trigger_summary("c1", ROUTE)
    assert http.call_args.args == (
        "POST",
        "http://studio-api/api/conversations/c1/download",
    )
    assert http.call_args.kwargs["params"] == {"format": ROUTE, "regenerate": "true"}


def test_delete_tolerates_a_missing_conversation(http):
    """A conversation already gone is fine; another error is raised."""
    http.return_value = _response(404)
    linto_catchup.StudioClient().delete("c1")
    http.return_value = _response(500)
    with pytest.raises(linto_catchup.CatchupError):
        linto_catchup.StudioClient().delete("c1")


def test_a_rejected_credential_is_renewed_once():
    """A 401 renews the credential and retries once."""
    with (
        mock.patch.object(
            linto_catchup.StudioClient, "_token", side_effect=["old", "new"]
        ) as token,
        mock.patch.object(
            linto_catchup.requests,
            "request",
            side_effect=[_response(401), _response(200, {"jobs": {}})],
        ) as request,
    ):
        assert linto_catchup.StudioClient().conversation("c1") == {"jobs": {}}
    assert token.call_args_list == [mock.call(False), mock.call(True)]
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer new"}


def test_studio_unreachable_is_a_catchup_error(http):
    """A network error becomes a CatchupError."""
    http.side_effect = linto_catchup.requests.ConnectionError("down")
    with pytest.raises(linto_catchup.CatchupError):
        linto_catchup.StudioClient().conversation("c1")


def test_the_end_of_the_recording_keeps_the_conversation(active_transcript, studio):
    """The end of the recording keeps the conversation for the transcription."""
    _hold(active_transcript, T0 + timedelta(seconds=60))
    _running(active_transcript)
    with mock.patch.object(linto_catchup, "stop_captures") as stop:
        linto_catchup.on_recording_ended(active_transcript)
    stop.assert_called_once_with(str(active_transcript.room_id), active_transcript.id)
    studio.delete.assert_not_called()
    assert linto_catchup.load_state(active_transcript.id)["job"] is None
    assert _held(active_transcript)["conversation_id"] == "conv-1"


def _hand_over(recording, segments=12):
    with _segments_in_bucket(segments):
        return linto_catchup.hand_over(recording)


def test_hand_over_sends_the_last_minutes_and_finishes(active_transcript, studio):
    """The last minutes are sent, then the conversation is closed."""
    _hold(active_transcript, T0 + timedelta(seconds=60))
    assert _hand_over(active_transcript) == "conv-1"

    keys = [f"catchup/r/seg_{i:05d}.ts" for i in range(6, 12)]
    studio.append.assert_called_once_with("conv-1", b"".join(k.encode() for k in keys))
    studio.complete.assert_called_once_with("conv-1")
    assert _held(active_transcript)["covered_until"] == _iso(
        T0 + timedelta(seconds=120)
    )


def test_a_replayed_hand_over_sends_nothing_twice(active_transcript, studio):
    """Studio failed to finish: the retry finishes without sending again."""
    _hold(active_transcript, T0 + timedelta(seconds=60))
    studio.complete.side_effect = [
        linto_catchup.StudioHTTPError("finish: HTTP 503", 503, ""),
        None,
    ]
    with pytest.raises(linto_catchup.StudioHTTPError):
        _hand_over(active_transcript)
    active_transcript.refresh_from_db()
    assert _hand_over(active_transcript) == "conv-1"
    assert studio.append.call_count == 1
    assert studio.complete.call_count == 2


def test_hand_over_without_conversation_leaves_the_upload(active_transcript, studio):
    """No late joiner opened a conversation: the whole audio is uploaded."""
    assert _hand_over(active_transcript) is None
    studio.append.assert_not_called()
    studio.complete.assert_not_called()


def test_a_refused_hand_over_falls_back_to_the_upload(active_transcript, studio):
    """A conversation deleted meanwhile falls back to the full upload."""
    _hold(active_transcript, T0 + timedelta(seconds=60))
    studio.append.side_effect = linto_catchup.StudioHTTPError(
        "append: HTTP 404", 404, "Conversation not found"
    )
    assert _hand_over(active_transcript) is None
    studio.delete.assert_called_once_with("conv-1")
    assert not _held(active_transcript)


def test_hand_over_waits_for_a_late_joiner_sending(active_transcript, studio):
    """A late joiner's audio is being sent: the hand-over is retried later."""
    _hold(active_transcript, T0 + timedelta(seconds=60))
    cache.add(linto_catchup._lock_key(active_transcript.id, "start"), 1, 60)
    try:
        with pytest.raises(linto_catchup.CatchupError):
            _hand_over(active_transcript)
    finally:
        cache.delete(linto_catchup._lock_key(active_transcript.id, "start"))
    studio.complete.assert_not_called()


def test_drop_conversation_deletes_and_forgets(active_video, studio):
    """Next to a video, the briefs' conversation goes at the end."""
    _hold(active_video, T0 + timedelta(seconds=60))
    linto_catchup.drop_conversation(active_video)
    studio.delete.assert_called_once_with("conv-1")
    assert not _held(active_video)


def test_recording_audio_is_every_segment(active_transcript):
    """The file of a segmented recording is made of all its segments."""
    with (
        mock.patch.object(linto_catchup, "list_segments", return_value=_segments(3)),
        mock.patch.object(linto_catchup, "read_segments", return_value=b"TS") as read,
    ):
        assert linto_catchup.recording_audio(active_transcript) == b"TS"
    assert read.call_args.args[0] == [f"catchup/r/seg_{i:05d}.ts" for i in range(3)]


def test_reconcile_discards_the_state_of_ended_recordings(studio):
    """A state left behind by an ended recording is discarded."""
    ended = RecordingFactory(mode="transcript", status="stopped")
    active = RecordingFactory(mode="transcript", status="active")
    _running(ended)
    _running(active)
    with (
        mock.patch.object(linto_catchup, "_list_active_egresses", return_value=[]),
        mock.patch.object(linto_catchup, "list_prefixes", return_value=[]),
    ):
        result = linto_catchup.reconcile()
    assert result["discarded"] >= 1
    assert linto_catchup.load_state(ended.id)["job"] is None
    assert linto_catchup.load_state(active.id)["job"] is not None


def test_reconcile_keeps_segments_until_the_transcription_read_them():
    """Segments stay while a transcription may still read them, two days at most."""
    waiting = RecordingFactory(mode="transcript", status="notification_succeeded")
    failed = RecordingFactory(
        mode="transcript", status="notification_failed", options={"segments": True}
    )
    failed_video = RecordingFactory(
        mode="screen_recording", status="notification_failed"
    )
    old = RecordingFactory(mode="transcript", status="saved")
    old.created_at = linto_catchup._now() - timedelta(days=3)
    old.save()
    prefixes = [
        str(waiting.id),
        str(failed.id),
        str(failed_video.id),
        str(old.id),
        "not-a-uuid",
    ]
    with (
        mock.patch.object(linto_catchup, "_list_active_egresses", return_value=[]),
        mock.patch.object(linto_catchup, "list_prefixes", return_value=prefixes),
        mock.patch.object(linto_catchup, "delete_segments") as delete,
    ):
        result = linto_catchup.reconcile()
    deleted = {call.args[0] for call in delete.call_args_list}
    assert deleted == {str(failed_video.id), str(old.id), "not-a-uuid"}
    assert result["deleted"] == 3


def test_reconcile_stops_only_the_egresses_of_ended_recordings():
    """A recording starting (egress running, not saved ACTIVE yet) keeps its
    egress for a while; an ended, stuck or unknown one loses it."""
    stuck = RecordingFactory(mode="transcript", status="initiated")
    stuck.created_at = linto_catchup._now() - timedelta(minutes=5)
    stuck.save()
    egresses = [
        _egress(f"catchup/{r.id}/seg", f"EG_{r.status}")
        for r in (
            RecordingFactory(mode="transcript", status=status)
            for status in ("initiated", "active", "stopped")
        )
    ] + [
        _egress(f"catchup/{stuck.id}/seg", "EG_stuck"),
        _egress("catchup/not-a-uuid/seg", "EG_foreign"),
    ]
    with (
        mock.patch.object(
            linto_catchup, "_list_active_egresses", return_value=egresses
        ),
        mock.patch.object(linto_catchup, "_stop_egress") as stop,
        mock.patch.object(linto_catchup, "list_prefixes", return_value=[]),
    ):
        result = linto_catchup.reconcile()
    assert {call.args[0] for call in stop.call_args_list} == {
        "EG_stopped",
        "EG_stuck",
        "EG_foreign",
    }
    assert result["stopped"] == 3


def test_the_reconcile_task_runs_one_pass():
    """Celery Beat's task runs the reconciliation and answers its counts."""
    counts = {"stopped": 1, "deleted": 2, "discarded": 0}
    with mock.patch.object(linto_catchup, "reconcile", return_value=counts):
        assert reconcile_linto_catchup() == counts


def test_the_reconcile_task_survives_a_failure():
    """LiveKit or the bucket down: the run is logged and given up."""
    with mock.patch.object(
        linto_catchup, "reconcile", side_effect=linto_catchup.CatchupError("down")
    ):
        assert reconcile_linto_catchup() is None


def test_the_reconcile_task_does_nothing_when_off(settings):
    """Without the catch-up, the task calls nothing."""
    settings.LINTO_CATCHUP_ENABLED = False
    with mock.patch.object(linto_catchup, "reconcile") as reconcile:
        assert reconcile_linto_catchup() is None
    reconcile.assert_not_called()


def _bucket(key_count=0, error=None):
    client = mock.MagicMock()
    client.list_objects_v2.side_effect = error
    client.list_objects_v2.return_value = {
        "Contents": [{"Key": "seg_00000.ts"}] * key_count
    }
    return mock.patch.object(linto_catchup, "_s3", return_value=client)


def test_has_segments_reads_the_bucket():
    """A segmented recording has segments once one reached the bucket; a
    listing error does not give its audio up."""
    recording = RecordingFactory(mode="transcript", options={"segments": True})
    with _bucket(1) as s3:
        assert linto_catchup.has_segments(recording) is True
    assert s3.return_value.list_objects_v2.call_args.kwargs["Prefix"] == (
        f"catchup/{recording.id}/seg_"
    )
    with _bucket(0):
        assert linto_catchup.has_segments(recording) is False
    error = botocore.exceptions.EndpointConnectionError(endpoint_url="http://s3")
    with _bucket(error=error):
        assert linto_catchup.has_segments(recording) is True


def test_a_recording_with_its_own_file_has_no_segments():
    """A recording written as one file is not looked up in the bucket."""
    recording = RecordingFactory(mode="transcript")
    with _bucket(1) as s3:
        assert linto_catchup.has_segments(recording) is False
    s3.assert_not_called()


# ── Webhooks ────────────────────────────────────────────────────────────────


@pytest.fixture
def events_service():
    """LiveKit events service."""
    return LiveKitEventsService()


@mock.patch.object(api.WebhookReceiver, "receive")
def test_catchup_egress_events_are_acknowledged(mock_receive, events_service):
    """The side capture is not a Recording: its events must not fail (500)."""
    data = api.WebhookEvent(event="egress_updated")
    data.egress_info.CopyFrom(_egress("catchup/r1/seg"))
    data.egress_info.room_name = "11111111-1111-1111-1111-111111111111"
    mock_receive.return_value = data
    request = mock.MagicMock(headers={"Authorization": "token"}, body=b"{}")

    events_service.receive(request)  # no ActionFailedError


@mock.patch("core.services.room_management.RoomManagement.update_metadata")
@mock.patch.object(api.WebhookReceiver, "receive")
def test_a_segmented_recording_egress_updates_the_recording(
    mock_receive, _metadata, events_service
):
    """A segmented recording's egress events reach the recording handlers."""
    recording = RecordingFactory(
        worker_id="EG_rec",
        mode="transcript",
        status="initiated",
        options={"segments": True},
    )
    data = api.WebhookEvent(event="egress_updated")
    data.egress_info.CopyFrom(_egress(f"catchup/{recording.id}/seg", "EG_rec"))
    data.egress_info.status = api.EgressStatus.EGRESS_ACTIVE
    data.egress_info.room_name = str(recording.room_id)
    mock_receive.return_value = data
    request = mock.MagicMock(headers={"Authorization": "token"}, body=b"{}")

    with mock.patch.object(
        events_service.recording_events, "handle_update"
    ) as handle_update:
        events_service.receive(request)
    handle_update.assert_called_once()


def test_a_segmented_recording_starts_no_side_capture(active_transcript):
    """Its own egress writes the segments: no second egress."""
    with mock.patch.object(linto_catchup, "_start_segments_egress") as start:
        assert linto_catchup.start_capture(active_transcript) is None
    start.assert_not_called()


@mock.patch("core.services.room_management.RoomManagement.update_metadata")
@mock.patch.object(linto_catchup, "start_capture")
def test_main_egress_active_starts_capture(mock_start, _metadata, events_service):
    """A recording reaching ACTIVE asks for its capture."""
    recording = RecordingFactory(
        worker_id="EG_main",
        mode="screen_recording",
        status="active",
        options={"catchup": True, "transcribe": True},
    )
    data = mock.MagicMock()
    data.egress_info.egress_id = "EG_main"
    data.egress_info.status = api.EgressStatus.EGRESS_ACTIVE

    with mock.patch(
        "core.tasks.linto_catchup.start_linto_catchup_capture.apply_async"
    ) as delayed:
        events_service._handle_egress_updated(data)

    mock_start.assert_not_called()
    assert delayed.call_args.kwargs["args"] == [str(recording.id)]


@mock.patch("core.services.room_management.RoomManagement.update_metadata")
@mock.patch.object(linto_catchup, "start_capture")
def test_plain_screen_recording_starts_nothing(mock_start, _metadata, events_service):
    """A video recording without transcription gets no capture."""
    RecordingFactory(worker_id="EG_main", mode="screen_recording", status="active")
    data = mock.MagicMock()
    data.egress_info.egress_id = "EG_main"
    data.egress_info.status = api.EgressStatus.EGRESS_ACTIVE

    events_service._handle_egress_updated(data)

    mock_start.assert_not_called()


@mock.patch("core.services.room_management.RoomManagement.update_metadata")
@mock.patch.object(linto_catchup, "on_recording_ended")
def test_main_egress_ended_stops_capture(mock_ended, _metadata, events_service):
    """Whatever the reason the main egress ends, the capture stops with it."""
    recording = RecordingFactory(
        worker_id="EG_main", mode="transcript", status="active"
    )
    data = mock.MagicMock()
    data.egress_info.egress_id = "EG_main"
    data.egress_info.status = api.EgressStatus.EGRESS_FAILED
    # The recording's own end is not under test here.
    events_service.recording_events = mock.MagicMock()

    events_service._handle_egress_ended(data)

    mock_ended.assert_called_once()
    assert mock_ended.call_args.args[0].id == recording.id


def _end_segmented(events_service, status, segments):
    """A segmented recording's egress ends with `status`, segments in the bucket or not."""
    recording = RecordingFactory(
        worker_id="EG_rec",
        mode="transcript",
        status="active",
        options={"segments": True},
    )
    data = mock.MagicMock()
    data.egress_info.egress_id = recording.worker_id
    data.egress_info.status = status
    with (
        mock.patch.object(linto_catchup, "has_segments", return_value=segments),
        mock.patch.object(linto_catchup, "on_recording_ended"),
    ):
        events_service._handle_egress_ended(data)
    recording.refresh_from_db()
    return recording


@mock.patch("core.services.room_management.RoomManagement.update_metadata")
@mock.patch("core.recording.services.recording_events.utils.notify_participants")
@mock.patch(
    "core.recording.services.recording_events.notification_service"
    ".notify_and_update_status"
)
def test_a_failed_segmented_recording_keeps_its_segments(
    notify, participants, _metadata, events_service
):
    """The egress failed after writing segments: the recording is interrupted,
    saved and transcribed up to the failure."""
    recording = _end_segmented(events_service, api.EgressStatus.EGRESS_FAILED, True)

    assert recording.status == "stopped"
    notify.assert_called_once()
    assert participants.call_args.kwargs["notification_data"] == {
        "type": "transcriptionInterrupted"
    }


@pytest.mark.parametrize(
    ("status", "segments", "event"),
    (
        (api.EgressStatus.EGRESS_FAILED, False, RecordingWorkerEvent.FAILED),
        (api.EgressStatus.EGRESS_ABORTED, True, RecordingWorkerEvent.ABORTED),
        (api.EgressStatus.EGRESS_COMPLETE, True, RecordingWorkerEvent.COMPLETED),
    ),
)
@mock.patch("core.services.room_management.RoomManagement.update_metadata")
def test_only_a_failure_after_segments_is_interrupted(
    _metadata, status, segments, event, events_service
):
    """Without segments a failure stays a failure; other ends are unchanged."""
    events_service.recording_events = mock.MagicMock()
    _end_segmented(events_service, status, segments)
    events_service.recording_events.handle_terminal_event.assert_called_once_with(
        mock.ANY, event
    )
