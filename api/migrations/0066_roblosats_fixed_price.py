from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0065_roblosats_lightning_taker"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="fixed_price",
            field=models.DecimalField(
                blank=True,
                decimal_places=8,
                default=None,
                max_digits=22,
                null=True,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
            ),
        ),
    ]
