# Roblosats mixta C/D (26 nov 2026): a Lightning order taken with an onchain bond.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0072_roblosats_bond_penalty_ladder"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="maker_lightning",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name="takeorder",
            name="onchain",
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]
