# Roblosats 0.9.17: signed actions (GHSA-22q8-9ghc-jg3h). Only adds two nullable columns.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0075_roblosats_market_tick_precision"),
    ]

    operations = [
        migrations.AddField(
            model_name="robot",
            name="signs_actions_since",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="robot",
            name="unsigned_warned_at",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
    ]
