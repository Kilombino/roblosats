from django.db import migrations, models


class Migration(migrations.Migration):
    """rl31-beta18: onchain bonds paid in several coins (top-up) and coins at a bond address
    that were not the bond, sent back to their owner."""

    dependencies = [
        ("api", "0066_roblosats_fixed_price"),
    ]

    operations = [
        migrations.AddField(model_name="onchainbond", name="coins", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="onchainbond", name="penalty_owner_sigs", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="onchainbond", name="return_coins", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="onchainbond", name="leftover_tx", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="onchainbond", name="leftover_coins", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="onchainbond", name="leftover_txids", field=models.JSONField(blank=True, default=list)),
    ]
