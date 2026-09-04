from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("surveys", "0039_finalidupload_auto_rejected_count")]

    operations = [
        migrations.AlterField(
            model_name="exportjob",
            name="kind",
            field=models.CharField(
                choices=[
                    ("projects", "Projects"),
                    ("traffic", "Traffic reports"),
                    ("terms", "Term reports"),
                    ("panelist", "Panelist data"),
                    ("user_dashboard", "User dashboard"),
                ],
                max_length=16,
            ),
        ),
    ]
