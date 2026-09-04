from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("surveys", "0038_finalidupload_invalid_count")]

    operations = [
        migrations.AddField(
            model_name="finalidupload",
            name="auto_rejected_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
