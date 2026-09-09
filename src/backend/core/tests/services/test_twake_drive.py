"""Tests for the Twake Drive "magic folder" lookup in core.services.twake_drive.

The meetings folder is identified by a `referenced_by` entry rather than by
its path, so a user renaming or moving `_Meetings` must not make the service
create a second folder. These tests pin that contract without hitting the
network: the HTTP helpers are patched and only the orchestration is exercised.
"""

# pylint: disable=redefined-outer-name

from datetime import datetime, timezone
from unittest import mock

import pytest
from asgiref.sync import async_to_sync

from core.services import twake_drive

RECORDING_ID = "4c3d9f1e-1234-4abc-9def-000000000001"
ROOM_ID = "9a8b7c6d-0000-4000-8000-000000000002"


def _directory(dir_id, path, created_at):
    return {
        "type": "io.cozy.files",
        "id": dir_id,
        "attributes": {
            "type": "directory",
            "path": path,
            "created_at": created_at,
        },
    }


@pytest.fixture
def recording():
    """A recording of a room, created on 2026-09-08."""
    return mock.Mock(
        id=RECORDING_ID,
        room_id=ROOM_ID,
        created_at=datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc),
    )


def test_references_url_encodes_slash_in_document_id():
    """The document id `io.cozy.apps/meet` must be a single path segment."""
    url = twake_drive._references_url(  # pylint: disable=protected-access
        "instance.test", {"type": "io.cozy.apps", "id": "io.cozy.apps/meet"}
    )
    assert url == (
        "https://instance.test/data/io.cozy.apps/io.cozy.apps%2Fmeet"
        "/relationships/references"
    )


def test_pick_live_directory_ignores_trashed_and_files():
    """Trashed folders and plain files are skipped, most recent folder wins."""
    included = [
        _directory("old", "/_Meetings", "2024-01-01T00:00:00Z"),
        _directory("trashed", "/.cozy_trash/_Meetings", "2025-01-01T00:00:00Z"),
        {
            "type": "io.cozy.files",
            "id": "file",
            "attributes": {"type": "file", "created_at": "2026-01-01T00:00:00Z"},
        },
        _directory("recent", "/Perso/Réunions", "2025-06-01T00:00:00Z"),
    ]
    assert twake_drive.pick_live_directory(included)["id"] == "recent"


def test_pick_live_directory_returns_none_when_nothing_matches():
    """No directory at all, or only trashed ones, means no magic folder."""
    assert twake_drive.pick_live_directory([]) is None
    trashed = [_directory("t", "/.cozy_trash/_Meetings", "2025-01-01T00:00:00Z")]
    assert twake_drive.pick_live_directory(trashed) is None


def test_build_meeting_dirname_uses_date_time_and_room_id(recording):
    """Folder name is `Meeting - {YYYY MM DD HHMM} - {room id}`."""
    assert twake_drive.meeting_date(recording) == "2026 09 08"
    assert (
        twake_drive.build_meeting_dirname(recording)
        == f"Meeting - 2026 09 08 1430 - {ROOM_ID}"
    )


@pytest.mark.parametrize(
    "language, expected",
    [
        ("fr-fr", "Réunion"),
        ("fr", "Réunion"),
        ("FR_FR", "Réunion"),
        ("en-us", "Meeting"),
        ("de-de", "Meeting"),
        ("", "Meeting"),
        (None, "Meeting"),
    ],
)
def test_names_follow_the_language_with_english_fallback(recording, language, expected):
    """French gets French labels, anything else falls back to English."""
    assert twake_drive.build_meeting_dirname(recording, language).startswith(
        f"{expected} - "
    )


def test_file_names_are_localized(recording):
    """Summary and transcript names carry the date and a localized label."""
    assert (
        twake_drive.build_summary_filename(recording, "docx", "fr-fr")
        == "2026 09 08 - Résumé.docx"
    )
    assert (
        twake_drive.build_summary_filename(recording, "pdf")
        == "2026 09 08 - Summary.pdf"
    )
    assert (
        twake_drive.build_transcript_filename(recording, "fr-fr")
        == "2026 09 08 - Transcription.cozy-note"
    )
    assert (
        twake_drive.build_transcript_filename(recording, "nl-nl")
        == "2026 09 08 - Transcript.cozy-note"
    )
    assert (
        twake_drive.build_recording_filename(recording, "ogg", "fr-fr")
        == "2026 09 08 - Enregistrement.ogg"
    )
    assert (
        twake_drive.build_recording_filename(recording, "mp4")
        == "2026 09 08 - Recording.mp4"
    )
    assert (
        twake_drive.build_linto_shortcut_filename("fr-fr")
        == "Plus de détails dans LinTO.url"
    )
    assert twake_drive.build_linto_shortcut_filename() == "More details in LinTO.url"


@pytest.fixture
def drive():
    """Patch the HTTP layer of the service."""
    with (
        mock.patch.object(
            twake_drive, "get_referenced_directory", new_callable=mock.AsyncMock
        ) as get_referenced,
        mock.patch.object(
            twake_drive, "ensure_directory", new_callable=mock.AsyncMock
        ) as ensure,
        mock.patch.object(
            twake_drive, "add_reference", new_callable=mock.AsyncMock
        ) as add_ref,
    ):
        yield {
            "get_referenced": get_referenced,
            "ensure_directory": ensure,
            "add_reference": add_ref,
        }


def test_ensure_meetings_directory_reuses_renamed_magic_folder(drive):
    """A referenced meetings folder is used as is, even if renamed or moved."""
    drive["get_referenced"].return_value = {"id": "root-id", "path": "/Perso/Réunions"}

    meetings_dir = async_to_sync(twake_drive.ensure_meetings_directory)(
        "instance.test", "token"
    )

    assert meetings_dir == {"id": "root-id", "path": "/Perso/Réunions"}
    drive["ensure_directory"].assert_not_awaited()
    drive["add_reference"].assert_not_awaited()


@pytest.mark.parametrize(
    "language, root",
    [("fr-fr", "/_Réunions"), ("en-us", "/_Meetings"), (None, "/_Meetings")],
)
def test_ensure_meetings_directory_creates_and_tags_default_folder(
    drive, language, root
):
    """Without a referenced folder, the localized root is created and tagged."""
    drive["get_referenced"].return_value = None
    drive["ensure_directory"].return_value = "root-id"

    meetings_dir = async_to_sync(twake_drive.ensure_meetings_directory)(
        "instance.test", "token", language
    )

    assert meetings_dir == {"id": "root-id", "path": root}
    drive["ensure_directory"].assert_awaited_once_with(
        "instance.test", "token", root, twake_drive.ROOT_DIR_ID, favorite=True
    )
    drive["add_reference"].assert_awaited_once_with(
        "instance.test", "token", twake_drive.MEETINGS_DIR_REFERENCE, "root-id"
    )


def test_ensure_meeting_directory_nests_under_the_meetings_folder(drive, recording):
    """The meeting folder is created by path under the (possibly renamed) root."""
    drive["get_referenced"].return_value = {"id": "root-id", "path": "/Perso/Réunions"}
    drive["ensure_directory"].return_value = "meeting-id"

    dir_id = async_to_sync(twake_drive.ensure_meeting_directory)(
        "instance.test", "token", recording, language="fr-fr"
    )

    assert dir_id == "meeting-id"
    drive["ensure_directory"].assert_awaited_once_with(
        "instance.test",
        "token",
        f"/Perso/Réunions/Réunion - 2026 09 08 1430 - {ROOM_ID}",
        "root-id",
    )
