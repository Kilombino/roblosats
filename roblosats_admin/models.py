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
    "on_bond_manual",
    "on_onchain_events",
    "on_wallet_events",
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
    on_bond_manual = models.BooleanField(default=False)  # 0.9.7
    on_onchain_events = models.BooleanField(default=False)  # 0.9.7, watch.py
    on_wallet_events = models.BooleanField(default=False)  # 0.9.7, watch.py
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _lazy("Telegram alerts")
        verbose_name_plural = _lazy("Telegram alerts")

    def __str__(self):
        return "Telegram alerts"


class AlertMark(models.Model):
    """An event the watcher (watch.py) has already seen: told once, never again."""

    key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _lazy("Alert already sent")
        verbose_name_plural = _lazy("Alerts already sent")

    def __str__(self):
        return self.key
