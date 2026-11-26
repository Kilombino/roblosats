# Roblosats (26 nov 2026): the chat attached to a dispute after the statement.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0073_roblosats_onchain_take_of_ln_order"),
    ]

    operations = [
        migrations.AddField(model_name="order", name="maker_chat_log", field=models.TextField(blank=True, default=None, null=True)),
        migrations.AddField(model_name="order", name="taker_chat_log", field=models.TextField(blank=True, default=None, null=True)),
        migrations.AddField(model_name="order", name="maker_chat_log_at", field=models.DateTimeField(blank=True, default=None, null=True)),
        migrations.AddField(model_name="order", name="taker_chat_log_at", field=models.DateTimeField(blank=True, default=None, null=True)),
    ]
