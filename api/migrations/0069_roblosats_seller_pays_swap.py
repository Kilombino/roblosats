from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0068_roblosats_sats_orders"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="swap_surcharge_sats",
            field=models.PositiveBigIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="order",
            name="swap_surcharge_refund_sats",
            field=models.PositiveBigIntegerField(db_default=0, default=0),
        ),
    ]
