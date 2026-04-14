"""Recordings started by an authenticated participant without a room role.

Who may start or stop a recording is governed by `HasRecordingPermission`
(tested in test_api_rooms_recording_permissions.py): the `authenticated` level
lets any authenticated user record. These tests cover the accesses granted on
such a recording.
"""

# pylint: disable=redefined-outer-name,unused-argument,protected-access

from unittest import mock

import pytest
from rest_framework.test import APIClient

from core.factories import RecordingFactory, RoomFactory, UserFactory
from core.models import (
    Recording,
    RecordingStatusChoices,
    RoleChoices,
    RoomAccessLevel,
)
from core.recording.event.notification import NotificationService

pytestmark = pytest.mark.django_db


@pytest.fixture
def mock_worker_manager():
    """Mock the worker service factory and mediator."""
    with (
        mock.patch("core.api.viewsets.get_worker_service"),
        mock.patch("core.api.viewsets.WorkerServiceMediator") as mediator_class,
    ):
        mediator = mock.Mock()
        mediator_class.return_value = mediator
        yield mediator


@pytest.fixture(autouse=True)
def open_recording_to_authenticated_users(settings):
    """Recording is enabled and open to any authenticated user, in both modes."""
    settings.RECORDING_ENABLE = True
    settings.RECORDING_SCREEN_PERMISSION = "authenticated"
    settings.RECORDING_TRANSCRIPT_PERMISSION = "authenticated"


@pytest.mark.parametrize(
    "access_level", [RoomAccessLevel.TRUSTED, RoomAccessLevel.PUBLIC]
)
def test_authenticated_user_can_start_recording(access_level, mock_worker_manager):
    """An authenticated user without a room role can start: they are member."""
    user = UserFactory()
    room = RoomFactory(access_level=access_level)

    client = APIClient()
    client.force_login(user)

    response = client.post(
        f"/api/v1.0/rooms/{room.id}/start-recording/", {"mode": "screen_recording"}
    )

    assert response.status_code == 201
    recording = Recording.objects.get()
    mock_worker_manager.start.assert_called_once_with(recording)
    access = recording.accesses.get()
    assert access.user == user
    assert access.role == RoleChoices.MEMBER


@pytest.mark.parametrize(
    "access_level", [RoomAccessLevel.TRUSTED, RoomAccessLevel.PUBLIC]
)
def test_anonymous_forbidden(access_level):
    """Anonymous users never record, even on public rooms."""
    room = RoomFactory(access_level=access_level)
    response = APIClient().post(
        f"/api/v1.0/rooms/{room.id}/start-recording/", {"mode": "screen_recording"}
    )
    assert response.status_code == 401


@mock.patch(
    "core.services.participants_management.ParticipantsManagement.check_if_in_meeting",
    return_value=True,
)
def test_public_room_lobby_is_open_but_empty(mock_check):
    """Participants pass the lobby permission on public rooms, which have no lobby."""
    room = RoomFactory(access_level=RoomAccessLevel.PUBLIC)
    user = UserFactory()
    client = APIClient()
    client.force_login(user)

    listing = client.get(f"/api/v1.0/rooms/{room.id}/waiting-participants/")
    entry = client.post(
        f"/api/v1.0/rooms/{room.id}/enter/",
        {"participant_id": "2f7f162f-e7d1-421b-90e7-02bfbfbf8def", "allow_entry": True},
    )

    assert listing.status_code == 200
    assert listing.json() == {"participants": []}
    assert entry.status_code == 404
    assert entry.json() == {"message": "Room has no lobby system."}


def test_room_owners_and_admins_get_access_to_participant_recording(
    mock_worker_manager,
):
    """Room owners/admins own the recording, the starter is member; room members get nothing."""
    starter = UserFactory()
    room_owner = UserFactory()
    room_admin = UserFactory()
    room_member = UserFactory()
    room = RoomFactory(access_level=RoomAccessLevel.TRUSTED)
    room.accesses.create(user=room_owner, role=RoleChoices.OWNER)
    room.accesses.create(user=room_admin, role=RoleChoices.ADMIN)
    room.accesses.create(user=room_member, role=RoleChoices.MEMBER)
    client = APIClient()
    client.force_login(starter)
    response = client.post(
        f"/api/v1.0/rooms/{room.id}/start-recording/", {"mode": "screen_recording"}
    )

    assert response.status_code == 201
    recording = Recording.objects.get()
    assert dict(recording.accesses.values_list("user_id", "role")) == {
        starter.id: RoleChoices.MEMBER,
        room_owner.id: RoleChoices.OWNER,
        room_admin.id: RoleChoices.OWNER,
    }

    # Room admins can see and download it; plain members cannot.
    client.force_login(room_admin)
    assert client.get(f"/api/v1.0/recordings/{recording.id}/").status_code == 200
    client.force_login(room_member)
    assert client.get(f"/api/v1.0/recordings/{recording.id}/").status_code == 404


def test_room_admin_starter_is_not_duplicated(settings, mock_worker_manager):
    """A room admin who starts the recording gets an exclusive access."""
    settings.RECORDING_SCREEN_PERMISSION = "admin_owner"
    starter = UserFactory()
    co_owner = UserFactory()
    room = RoomFactory(access_level=RoomAccessLevel.RESTRICTED)
    room.accesses.create(user=starter, role=RoleChoices.ADMIN)
    room.accesses.create(user=co_owner, role=RoleChoices.OWNER)
    client = APIClient()
    client.force_login(starter)
    response = client.post(
        f"/api/v1.0/rooms/{room.id}/start-recording/", {"mode": "screen_recording"}
    )

    assert response.status_code == 201
    recording = Recording.objects.get()
    assert dict(recording.accesses.values_list("user_id", "role")) == {
        starter.id: RoleChoices.OWNER,
        co_owner.id: RoleChoices.OWNER,
    }


def test_participant_can_stop_recording_started_by_someone_else(mock_worker_manager):
    """Stopping follows the permission of the mode, not who started it."""
    room = RoomFactory(access_level=RoomAccessLevel.TRUSTED)
    recording = RecordingFactory(room=room, status=RecordingStatusChoices.ACTIVE)
    recording.accesses.create(user=UserFactory(), role=RoleChoices.OWNER)
    user = UserFactory()
    client = APIClient()
    client.force_login(user)

    response = client.post(f"/api/v1.0/rooms/{room.id}/stop-recording/")

    assert response.status_code == 200
    mock_worker_manager.stop.assert_called_once_with(recording)


def test_summary_goes_to_starter_among_several_owners(mock_worker_manager):
    """The starter's access is created first, so the summary goes to them."""
    starter = UserFactory()
    room = RoomFactory(access_level=RoomAccessLevel.TRUSTED)
    for _ in range(2):
        room.accesses.create(user=UserFactory(), role=RoleChoices.OWNER)
    client = APIClient()
    client.force_login(starter)

    response = client.post(
        f"/api/v1.0/rooms/{room.id}/start-recording/", {"mode": "transcript"}
    )

    assert response.status_code == 201
    recording = Recording.objects.get()
    assert recording.accesses.count() == 3
    assert NotificationService._get_summary_recipient(recording) == starter


def test_participant_starter_can_retrieve_but_not_delete(mock_worker_manager):
    """A starter without a privileged room role can download but not delete."""
    starter = UserFactory()
    room_owner = UserFactory()
    room = RoomFactory(access_level=RoomAccessLevel.TRUSTED)
    room.accesses.create(user=room_owner, role=RoleChoices.OWNER)
    client = APIClient()
    client.force_login(starter)

    response = client.post(
        f"/api/v1.0/rooms/{room.id}/start-recording/", {"mode": "screen_recording"}
    )
    assert response.status_code == 201
    recording = Recording.objects.get()
    recording.status = RecordingStatusChoices.SAVED
    recording.save()

    assert client.get(f"/api/v1.0/recordings/{recording.id}/").status_code == 200
    assert client.delete(f"/api/v1.0/recordings/{recording.id}/").status_code == 403
    assert Recording.objects.filter(id=recording.id).exists()

    client.force_login(room_owner)
    assert client.delete(f"/api/v1.0/recordings/{recording.id}/").status_code == 204
    assert not Recording.objects.filter(id=recording.id).exists()
