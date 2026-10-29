from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0024_user_twake_instance"),
    ]

    operations = [
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
                    ],
                    max_length=50,
                ),
                blank=True,
                default=list,
                size=None,
            ),
        ),
    ]
