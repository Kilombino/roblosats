from django.db import models

from roblosats_admin.i18n import _lazy

EVENT_FIELDS = (
    "on_published",
    "on_taken",
    "on_escrow_locked",
    "on_finished",
    "on_cancelled",
    "on_dispute",
    "on_dispute_chat",
    "on_coordinator_message",
)


class TelegramAlerts(models.Model):
    bot_token = models.CharField(max_length=200, blank=True, default="")
    chat_id = models.CharField(max_length=64, blank=True, default="")
    language = models.CharField(max_length=10, default="en")
    panel_url = models.CharField(max_length=200, blank=True, default="")
    on_published = models.BooleanField(default=False)
    on_taken = models.BooleanField(default=False)
    on_escrow_locked = models.BooleanField(default=False)
    on_finished = models.BooleanField(default=False)
    on_cancelled = models.BooleanField(default=False)
    on_dispute = models.BooleanField(default=False)
    on_dispute_chat = models.BooleanField(default=False)
    on_coordinator_message = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _lazy("Telegram alerts")
        verbose_name_plural = _lazy("Telegram alerts")

    def __str__(self):
        return "Telegram alerts"
