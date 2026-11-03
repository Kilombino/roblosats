from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="TelegramAlerts",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("bot_token", models.CharField(blank=True, default="", max_length=200)),
                ("chat_id", models.CharField(blank=True, default="", max_length=64)),
                ("language", models.CharField(default="en", max_length=10)),
                ("panel_url", models.CharField(blank=True, default="", max_length=200)),
                ("on_published", models.BooleanField(default=False)),
                ("on_taken", models.BooleanField(default=False)),
                ("on_escrow_locked", models.BooleanField(default=False)),
                ("on_finished", models.BooleanField(default=False)),
                ("on_cancelled", models.BooleanField(default=False)),
                ("on_dispute", models.BooleanField(default=False)),
                ("on_dispute_chat", models.BooleanField(default=False)),
                ("on_coordinator_message", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Telegram alerts",
                "verbose_name_plural": "Telegram alerts",
            },
        ),
    ]
