from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("surveys", "0037_final_id_reconciliation")]

    operations = [
        migrations.AddField(
            model_name="finalidupload",
            name="invalid_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
