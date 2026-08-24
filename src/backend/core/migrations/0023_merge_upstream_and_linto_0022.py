from django.db import migrations


class Migration(migrations.Migration):
    """Reunite the two 0022 branches into a single migration leaf.

    The upstream rebase brought in 0022_user_default_room_access_level_and_more,
    which hangs off the same parent (0021) as the fork's own
    0022_recording_linto_state_alter_recording_status. Two leaves make Django
    refuse to migrate.

    A merge migration is the only correct fix here. Making the fork's 0022
    depend on the upstream one instead looks tidier, but it breaks every
    already-deployed database: the fork's 0022 is recorded as applied there, so
    giving it a retroactive, unapplied dependency makes
    ``check_consistent_history`` raise InconsistentMigrationHistory and abort
    ``migrate`` before it does any work.
    """

    dependencies = [
        ("core", "0022_recording_linto_state_alter_recording_status"),
        ("core", "0022_user_default_room_access_level_and_more"),
    ]

    operations = []
