"""
Test rooms API endpoints in the Meet core app: stop recording.
"""

# pylint: disable=redefined-outer-name,unused-argument

from unittest import mock

import pytest
from rest_framework.test import APIClient

from ... import utils
from ...factories import RecordingFactory, RoomFactory, UserFactory
from ...models import Recording, RecordingStatusChoices
from ...recording.worker.exceptions import RecordingStopError
from ...services.participants_management import (
    ParticipantNotFoundException,
    ParticipantsManagementException,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def mock_worker_service():
    """Mock worker service."""
    return mock.Mock()


@pytest.fixture
def mock_worker_service_factory(mock_worker_service):
    """Mock worker service factory."""
    with mock.patch(
        "core.api.viewsets.get_worker_service",
        return_value=mock_worker_service,
    ) as mock_worker_service_factory:
        yield mock_worker_service_factory


@pytest.fixture
def mock_worker_manager(mock_worker_service):
    """Mock worker service mediator."""
    with mock.patch("core.api.viewsets.WorkerServiceMediator") as mock_mediator_class:
        mock_mediator = mock.Mock()
        mock_mediator_class.return_value = mock_mediator
        yield mock_mediator


@pytest.fixture
def mock_presence():
    """LiveKit's answer to « is this identity in the call? »."""
    with mock.patch("core.api.permissions.ParticipantsManagement") as pm:
        pm.return_value.check_if_in_meeting.return_value = True
        yield pm.return_value.check_if_in_meeting


def livekit_token_for(user, room):
    """The LiveKit token Meet mints for the user of an application."""
    return utils.generate_token(
        room=str(room.id),
        user=user,
        identity=f"@{user.sub}:localhost:DEVICE",
    )


def test_stop_recording_with_the_livekit_token_of_the_call(
    settings, mock_worker_service_factory, mock_worker_manager, mock_presence
):
    """A participant without a Meet session stops the recording with the token
    of the call, when the room lets any authenticated participant record."""
    settings.RECORDING_ENABLE = True
    user = UserFactory()
    room = RoomFactory(configuration={"screen_recording_permission": "authenticated"})
    RecordingFactory(room=room, status=RecordingStatusChoices.ACTIVE)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {livekit_token_for(user, room)}")

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    assert response.status_code == 200
    mock_worker_manager.stop.assert_called_once()


def test_stop_recording_with_a_livekit_token_of_another_room(settings):
    """A token of room X must not stop the recording of room Y."""
    settings.RECORDING_ENABLE = True
    user = UserFactory()
    other = RoomFactory(configuration={"screen_recording_permission": "authenticated"})
    room = RoomFactory(configuration={"screen_recording_permission": "authenticated"})
    RecordingFactory(room=room, status=RecordingStatusChoices.ACTIVE)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {livekit_token_for(user, other)}")

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    assert response.status_code == 403
    assert Recording.objects.filter(status=RecordingStatusChoices.ACTIVE).count() == 1


@pytest.mark.parametrize(
    "presence",
    [False, ParticipantNotFoundException(), ParticipantsManagementException()],
)
def test_stop_recording_with_a_livekit_token_out_of_the_call(
    settings, mock_worker_manager, mock_presence, presence
):
    """A token outlives a member the application removed: its holder must be
    in the call to stop the recording; a LiveKit error refuses too."""
    settings.RECORDING_ENABLE = True
    if isinstance(presence, Exception):
        mock_presence.side_effect = presence
    else:
        mock_presence.return_value = presence
    user = UserFactory()
    room = RoomFactory(configuration={"screen_recording_permission": "authenticated"})
    RecordingFactory(room=room, status=RecordingStatusChoices.ACTIVE)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {livekit_token_for(user, room)}")

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    assert response.status_code == 403
    mock_presence.assert_called_once_with(
        room_name=str(room.pk), identity=f"@{user.sub}:localhost:DEVICE"
    )
    mock_worker_manager.stop.assert_not_called()


def test_stop_recording_anonymous():
    """Anonymous users should not be allowed to stop room recordings."""
    room = RoomFactory()
    RecordingFactory(room=room, status=RecordingStatusChoices.ACTIVE)
    client = APIClient()

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    assert response.status_code == 401
    # Verify recording status hasn't changed
    assert Recording.objects.filter(status=RecordingStatusChoices.ACTIVE).count() == 1


def test_stop_recording_non_owner_and_non_administrator(settings):
    """Non-owner and Non-Administrator users should not be allowed to stop room recordings."""
    settings.RECORDING_ENABLE = True
    room = RoomFactory()
    user = UserFactory()
    RecordingFactory(room=room, status=RecordingStatusChoices.ACTIVE)
    client = APIClient()
    client.force_login(user)

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    assert response.status_code == 403
    # Verify recording status hasn't changed
    assert Recording.objects.filter(status=RecordingStatusChoices.ACTIVE).count() == 1


def test_stop_recording_recording_disabled(settings):
    """Should fail if recording is disabled for the room."""

    settings.RECORDING_ENABLE = False

    room = RoomFactory()
    user = UserFactory()
    # Make user the room owner
    room.accesses.create(user=user, role="owner")

    client = APIClient()
    client.force_login(user)

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found."}
    # Verify no recording exists
    assert Recording.objects.count() == 0


def test_stop_recording_no_active_recording(settings):
    """Should fail when there is no active recording for the room."""

    settings.RECORDING_ENABLE = True

    room = RoomFactory()
    user = UserFactory()
    # Make user the room owner
    room.accesses.create(user=user, role="owner")

    client = APIClient()
    client.force_login(user)

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    assert response.status_code == 404
    assert response.json() == {"detail": "No active recording found for this room."}


def test_stop_recording_worker_error(
    mock_worker_service_factory, mock_worker_manager, settings
):
    """Should handle worker service errors appropriately."""

    settings.RECORDING_ENABLE = True

    room = RoomFactory()
    user = UserFactory()
    recording = RecordingFactory(
        room=room,
        status=RecordingStatusChoices.ACTIVE,
        mode="screen_recording",
    )
    # Make user the room owner
    room.accesses.create(user=user, role="owner")

    # Configure mock mediator to raise error
    mock_stop = mock.Mock()
    mock_stop.side_effect = RecordingStopError("Failed to connect to worker")
    mock_worker_manager.stop = mock_stop

    client = APIClient()
    client.force_login(user)

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    mock_worker_service_factory.assert_called_once_with(mode="screen_recording")
    mock_stop.assert_called_once_with(recording)

    assert response.status_code == 500
    assert response.json() == {
        "error": f"Recording failed to stop for room {room.slug}"
    }
    # Verify recording status hasn't changed
    assert Recording.objects.filter(status=RecordingStatusChoices.ACTIVE).count() == 1


def test_stop_recording_success(
    mock_worker_service_factory, mock_worker_manager, settings
):
    """Should successfully stop recording when everything is configured correctly."""

    settings.RECORDING_ENABLE = True

    room = RoomFactory()
    user = UserFactory()
    recording = RecordingFactory(
        room=room,
        status=RecordingStatusChoices.ACTIVE,
        mode="screen_recording",
    )
    # Make user the room owner
    room.accesses.create(user=user, role="owner")

    mock_stop = mock.Mock()
    mock_worker_manager.stop = mock_stop

    client = APIClient()
    client.force_login(user)

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    mock_worker_service_factory.assert_called_once_with(mode="screen_recording")
    mock_stop.assert_called_once_with(recording)

    assert response.status_code == 200
    assert response.json() == {"message": f"Recording stopped for room {room.slug}."}

    # Verify the recording still exists
    assert Recording.objects.count() == 1
