# Roblosats 0.9.15: market ticks of 1 BTC or more (volume had 8 digits, all decimals) and Spamcoin
# prices per BTC (~0.0036, price had 2 decimals).
from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0074_roblosats_dispute_chat_log"),
    ]

    operations = [
        migrations.AlterField(
            model_name="markettick",
            name="price",
            field=models.DecimalField(decimal_places=8, default=None, max_digits=24, null=True,
                                      validators=[django.core.validators.MinValueValidator(Decimal("0"))]),
        ),
        migrations.AlterField(
            model_name="markettick",
            name="volume",
            field=models.DecimalField(decimal_places=8, default=None, max_digits=16, null=True,
                                      validators=[django.core.validators.MinValueValidator(Decimal("0"))]),
        ),
    ]
