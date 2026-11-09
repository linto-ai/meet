"""
Test subtitle service.
"""

# pylint: disable=W0621
from unittest import mock

import pytest
from livekit.api import TwirpError, TwirpErrorCode

from core.factories import RoomFactory
from core.services.subtitle import SubtitleService

pytestmark = pytest.mark.django_db


@pytest.fixture
def mock_livekit_client():
    """Mock LiveKit API client."""
    with mock.patch("core.utils.create_livekit_client") as mock_create:
        mock_client = mock.AsyncMock()
        mock_create.return_value = mock_client
        yield mock_client


def test_start_subtitle_settings(mock_livekit_client, settings):
    """Test that start_subtitle uses the configured agent name from Django settings."""

    settings.ROOM_SUBTITLE_AGENT_NAME = "fake-subtitle-agent-name"

    room = RoomFactory(name="my room")
    SubtitleService().start_subtitle(room)

    mock_livekit_client.agent_dispatch.create_dispatch.assert_called_once()

    call_args = mock_livekit_client.agent_dispatch.create_dispatch.call_args[0][0]
    assert call_args.agent_name == "fake-subtitle-agent-name"
    assert call_args.room == str(room.id)


def test_stop_subtitle_deletes_dispatch_and_evicts_agent(mock_livekit_client, settings):
    """stop_subtitle deletes the room's matching dispatch and evicts the agent."""

    settings.ROOM_SUBTITLE_AGENT_NAME = "multi-user-transcriber"

    room = RoomFactory(name="my room")

    matching = mock.Mock(id="disp-1", agent_name="multi-user-transcriber")
    other = mock.Mock(id="disp-2", agent_name="some-other-agent")
    mock_livekit_client.agent_dispatch.list_dispatch.return_value = [matching, other]

    SubtitleService().stop_subtitle(room)

    # list_dispatch is scoped to the room id.
    mock_livekit_client.agent_dispatch.list_dispatch.assert_called_once_with(
        room_name=str(room.id)
    )
    # Only the matching dispatch is deleted (the foreign agent is left alone).
    mock_livekit_client.agent_dispatch.delete_dispatch.assert_called_once_with(
        dispatch_id="disp-1", room_name=str(room.id)
    )
    # The hidden agent participant is evicted by its per-room identity.
    mock_livekit_client.room.remove_participant.assert_called_once()
    remove_arg = mock_livekit_client.room.remove_participant.call_args[0][0]
    assert remove_arg.room == str(room.id)
    assert remove_arg.identity == f"multi-user-transcriber-{room.id}"

    mock_livekit_client.aclose.assert_awaited_once()


def test_stop_subtitle_idempotent_when_nothing_exists(mock_livekit_client, settings):
    """With no dispatches, stop_subtitle deletes nothing yet still evicts + closes."""

    settings.ROOM_SUBTITLE_AGENT_NAME = "multi-user-transcriber"

    room = RoomFactory(name="my room")
    mock_livekit_client.agent_dispatch.list_dispatch.return_value = []

    # Must not raise even though there is nothing to delete.
    SubtitleService().stop_subtitle(room)

    mock_livekit_client.agent_dispatch.delete_dispatch.assert_not_called()
    mock_livekit_client.room.remove_participant.assert_called_once()
    mock_livekit_client.aclose.assert_awaited_once()


def test_stop_subtitle_is_best_effort_on_failures(mock_livekit_client, settings):
    """A failure of either lever is swallowed (non-fatal) and the client is closed."""

    settings.ROOM_SUBTITLE_AGENT_NAME = "multi-user-transcriber"

    room = RoomFactory(name="my room")
    mock_livekit_client.agent_dispatch.list_dispatch.side_effect = RuntimeError("boom")
    mock_livekit_client.room.remove_participant.side_effect = RuntimeError("boom")

    # Both levers fail, but stop_subtitle must not raise (best-effort).
    SubtitleService().stop_subtitle(room)

    mock_livekit_client.aclose.assert_awaited_once()


def test_stop_subtitle_agent_absent_is_not_a_warning(
    mock_livekit_client, settings, caplog
):
    """No subtitle agent in the room (the usual case when LinTO takes the CC
    over): LiveKit answers not_found to the eviction, which logs no warning."""

    settings.ROOM_SUBTITLE_AGENT_NAME = "multi-user-transcriber"

    room = RoomFactory(name="my room")
    mock_livekit_client.agent_dispatch.list_dispatch.return_value = []
    mock_livekit_client.room.remove_participant.side_effect = TwirpError(
        TwirpErrorCode.NOT_FOUND, "participant does not exist", status=404
    )

    with caplog.at_level("WARNING", logger="core.services.subtitle"):
        SubtitleService().stop_subtitle(room)

    assert not caplog.records
    mock_livekit_client.aclose.assert_awaited_once()


def test_stop_subtitle_other_eviction_errors_still_warn(
    mock_livekit_client, settings, caplog
):
    """Any other LiveKit answer to the eviction is still logged as a warning."""

    settings.ROOM_SUBTITLE_AGENT_NAME = "multi-user-transcriber"

    room = RoomFactory(name="my room")
    mock_livekit_client.agent_dispatch.list_dispatch.return_value = []
    mock_livekit_client.room.remove_participant.side_effect = TwirpError(
        TwirpErrorCode.INTERNAL, "boom", status=500
    )

    with caplog.at_level("WARNING", logger="core.services.subtitle"):
        SubtitleService().stop_subtitle(room)

    assert [r.getMessage() for r in caplog.records] == [
        f"Failed to evict subtitle agent participant for room {room.id}"
    ]
