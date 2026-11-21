from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0071_roblosats_settings_validators"),
    ]

    operations = [
        migrations.AddField(
            model_name="onchainbond",
            name="penalty_ladder",
            field=models.JSONField(
                blank=True, default=dict, db_default=models.Value({}, output_field=models.JSONField())
            ),
        ),
        migrations.AddField(
            model_name="onchainbond",
            name="wrong_chain",
            field=models.JSONField(
                blank=True, default=list, db_default=models.Value([], output_field=models.JSONField())
            ),
        ),
        migrations.AddField(
            model_name="onchainescrow",
            name="wrong_chain",
            field=models.JSONField(
                blank=True, default=list, db_default=models.Value([], output_field=models.JSONField())
            ),
        ),
    ]
