"""Tests for the Twake Drive "magic folder" lookup in core.services.twake_drive.

The meetings folder is identified by a `referenced_by` entry rather than by
its path, so a user renaming or moving `_Meetings` must not make the service
create a second folder. These tests pin that contract without hitting the
network: the HTTP helpers are patched and only the orchestration is exercised.
"""

# pylint: disable=redefined-outer-name

from unittest import mock

import pytest
from asgiref.sync import async_to_sync

from core.services import twake_drive


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


def test_ensure_meeting_directory_reuses_renamed_magic_folder(drive):
    """A referenced folder is used as is, even if renamed or moved."""
    drive["get_referenced"].return_value = {"id": "root-id", "path": "/Perso/Réunions"}
    drive["ensure_directory"].return_value = "meeting-id"

    dir_id = async_to_sync(twake_drive.ensure_meeting_directory)(
        "instance.test", "token", "Reunion_01-01-2026_10-00"
    )

    assert dir_id == "meeting-id"
    drive["ensure_directory"].assert_awaited_once_with(
        "instance.test", "token", "/Perso/Réunions/Reunion_01-01-2026_10-00", "root-id"
    )
    drive["add_reference"].assert_not_awaited()


def test_ensure_meeting_directory_creates_and_tags_default_folder(drive):
    """Without a referenced folder, `_Meetings` is created and tagged."""
    drive["get_referenced"].return_value = None
    drive["ensure_directory"].side_effect = ["root-id", "meeting-id"]

    dir_id = async_to_sync(twake_drive.ensure_meeting_directory)(
        "instance.test", "token", "Reunion_01-01-2026_10-00"
    )

    assert dir_id == "meeting-id"
    assert drive["ensure_directory"].await_args_list == [
        mock.call(
            "instance.test",
            "token",
            "/_Meetings",
            twake_drive.ROOT_DIR_ID,
            favorite=True,
        ),
        mock.call(
            "instance.test", "token", "/_Meetings/Reunion_01-01-2026_10-00", "root-id"
        ),
    ]
    drive["add_reference"].assert_awaited_once_with(
        "instance.test", "token", twake_drive.MEETINGS_DIR_REFERENCE, "root-id"
    )
