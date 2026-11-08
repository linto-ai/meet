from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):
    """Reunite the LinTO and upstream migration branches into a single leaf.

    The rebase on upstream 1.37 brought upstream 0023 to 0029, which hang off
    0022_user_default_room_access_level_and_more, while the fork's 0023 to 0026
    hang off its own merge migration. Both branches keep their names: deployed
    databases have applied the fork's migrations under them (see
    0023_merge_upstream_and_linto_0022 for why renaming or rechaining breaks
    ``check_consistent_history``).

    Both branches also redefine fields declared on both sides, the last
    AlterField of each branch listing only its own choices: the operations
    below give these fields their final choices, whichever branch Django
    applies last.
    """

    dependencies = [
        ("core", "0026_room_created_by_application"),
        ("core", "0029_alter_application_client_secret_sha256"),
    ]

    operations = [
        # LinTO 0025 adds rooms:livekit_token, upstream 0026 adds users:session
        migrations.AlterField(
            model_name="application",
            name="scopes",
            field=ArrayField(
                base_field=models.CharField(
                    choices=[
                        ("rooms:create", "Create rooms"),
                        ("rooms:list", "List rooms"),
                        ("rooms:retrieve", "Retrieve room details"),
                        ("rooms:update", "Update rooms"),
                        ("rooms:delete", "Delete rooms"),
                        ("rooms:livekit_token", "Mint LiveKit tokens"),
                        ("users:session", "Create user session tokens"),
                    ],
                    max_length=50,
                ),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        # LinTO 0022 adds notification_failed, upstream 0023 adds failed
        migrations.AlterField(
            model_name="recording",
            name="status",
            field=models.CharField(
                choices=[
                    ("initiated", "Initiated"),
                    ("active", "Active"),
                    ("stopped", "Stopped"),
                    ("saved", "Saved"),
                    ("aborted", "Aborted"),
                    ("failed", "Failed"),
                    ("failed_to_start", "Failed to Start"),
                    ("failed_to_stop", "Failed to Stop"),
                    ("notification_succeeded", "Notification succeeded"),
                    ("external_process_successful", "External process successful"),
                    ("external_process_failed", "External process failed"),
                    ("notification_failed", "Notification failed"),
                ],
                default="initiated",
                max_length=50,
            ),
        ),
    ]
