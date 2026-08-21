from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0027_surveyattempt_supplier_api_key_id_and_more"),
        ("vendors", "0021_alter_vendorapikey_client_allocations"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="verisoul_enabled",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Require a Verisoul risk decision before this client's prescreener is displayed.",
            ),
        ),
        migrations.AddField(
            model_name="organizationclientaccess",
            name="verisoul_mode",
            field=models.CharField(
                choices=[
                    ("inherit", "Inherit"),
                    ("enabled", "Require Verisoul"),
                    ("disabled", "Bypass Verisoul"),
                ],
                default="inherit",
                help_text="Inherit the closest parent/client setting, require Verisoul, or explicitly bypass it.",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="vendorclientallocation",
            name="verisoul_mode",
            field=models.CharField(
                choices=[
                    ("inherit", "Inherit"),
                    ("enabled", "Require Verisoul"),
                    ("disabled", "Bypass Verisoul"),
                ],
                default="inherit",
                help_text="Inherit the client setting, require Verisoul, or explicitly bypass it for this supplier.",
                max_length=12,
            ),
        ),
        migrations.CreateModel(
            name="VerisoulAssessment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("policy_scope", models.CharField(default="client", max_length=24)),
                ("policy_scope_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("session_id", models.CharField(blank=True, db_index=True, max_length=160)),
                ("request_id", models.CharField(blank=True, db_index=True, max_length=160)),
                ("project_id", models.CharField(blank=True, max_length=160)),
                ("decision", models.CharField(blank=True, db_index=True, max_length=40)),
                ("account_score", models.DecimalField(blank=True, decimal_places=6, max_digits=8, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("passed", "Passed"),
                            ("failed", "Failed"),
                            ("error", "Error"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=12,
                    ),
                ),
                ("reason", models.CharField(blank=True, max_length=240)),
                ("response_data", models.JSONField(blank=True, default=dict)),
                ("assessed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "attempt",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="verisoul_assessment",
                        to="surveys.surveyattempt",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="verisoul_assessments",
                        to="vendors.client",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="verisoulassessment",
            index=models.Index(fields=["client", "status", "-created_at"], name="verisoul_client_status_idx"),
        ),
    ]
