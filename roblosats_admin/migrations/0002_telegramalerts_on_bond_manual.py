from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("roblosats_admin", "0001_roblosats_telegram_alerts")]

    operations = [
        migrations.AddField(
            model_name="telegramalerts",
            name="on_bond_manual",
            field=models.BooleanField(default=False),
        ),
    ]
