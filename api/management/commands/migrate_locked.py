from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection

# Every service of a coordinator starts from the same image and runs migrate
# on start. Without a lock they apply the same migrations at once: on the
# v0.8.8 upgrade most of them failed and django_migrations got duplicate rows.
MIGRATE_LOCK_ID = 0x524F424C  # "ROBL"


class Command(BaseCommand):
    help = "Run migrate while holding a PostgreSQL advisory lock"

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [MIGRATE_LOCK_ID])
            try:
                call_command("migrate", interactive=False)
            finally:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [MIGRATE_LOCK_ID])
