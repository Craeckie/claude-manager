from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_event"),
    ]

    operations = [
        # Existing ideas keep verification; only ideas created from now on skip it.
        migrations.AddField(
            model_name="idea",
            name="needs_verification",
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name="idea",
            name="needs_verification",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Whether the finished idea waits in verify for the user to check it. If "
                    "false, finishing it (transition_idea to verify) closes it as implemented."
                ),
            ),
        ),
    ]
