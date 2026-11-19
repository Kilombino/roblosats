from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("roblosats_admin", "0001_roblosats_telegram_alerts")]

    operations = [
        migrations.AddField(
            model_name="telegramalerts",
            name="on_bond_manual",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="telegramalerts",
            name="on_onchain_events",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="telegramalerts",
            name="on_wallet_events",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="AlertMark",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=200, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name": "Alert already sent", "verbose_name_plural": "Alerts already sent"},
        ),
    ]
