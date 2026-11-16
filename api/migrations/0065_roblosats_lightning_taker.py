from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0064_roblosats_onchain_bond"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="taker_lightning",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name="takeorder",
            name="lightning",
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]
