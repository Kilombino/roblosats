from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0061_roblosats_order_logs_json"),
    ]

    operations = [
        migrations.AlterField(
            model_name="currency",
            name="exchange_rate",
            field=models.DecimalField(
                decimal_places=8,
                default=None,
                max_digits=22,
                null=True,
                validators=[django.core.validators.MinValueValidator(Decimal(0))],
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="spamcoin_sats",
            field=models.PositiveBigIntegerField(blank=True, default=None, null=True),
        ),
    ]
