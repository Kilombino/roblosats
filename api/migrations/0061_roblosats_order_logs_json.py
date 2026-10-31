# Roblosats: Order.logs from the HTML table of <= v0.8.7 to the JSON list of v0.8.8.
# Upstream (#2522/#2775) changed the format without converting old rows: the admin
# shows "No logs were recorded" for them and the next log() call overwrites the old
# history, which is the evidence of past disputes. Idempotent: JSON rows are skipped.
import html
import json
import re

from django.db import migrations

ROW = re.compile(r"<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>", re.S)


def plain(s):
    s = re.sub(r"</?b>", "**", s)
    s = re.sub(r"</?i>", "*", s)
    s = re.sub(r"<a [^>]*>(.*?)</a>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def html_logs_to_json(apps, schema_editor):
    Order = apps.get_model("api", "Order")
    rows = Order.objects.exclude(logs__isnull=True).exclude(logs="")
    for pk, raw in rows.values_list("pk", "logs").iterator():
        try:
            json.loads(raw)
            continue
        except (ValueError, TypeError):
            pass
        entries = [
            {"timestamp": plain(t), "level": plain(lv).strip("*"), "event": plain(e)}
            for t, lv, e in ROW.findall(raw)
        ]
        # Only if the row is still what was read, so a concurrent log() is never lost.
        Order.objects.filter(pk=pk, logs=raw).update(logs=json.dumps(entries))


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0060_lnpayment_rewarded_at"),
    ]

    operations = [
        migrations.RunPython(html_logs_to_json, migrations.RunPython.noop),
    ]
