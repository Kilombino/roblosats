from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0069_roblosats_seller_pays_swap"),
    ]

    operations = [
        migrations.AddField(
            model_name="onchainbond",
            name="auto_return",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
