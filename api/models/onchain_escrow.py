from django.db import models
from django.utils import timezone


class OnchainEscrow(models.Model):
    class Status(models.IntegerChoices):
        KEYS = 0, "Waiting for the traders' escrow keys"
        FUNDING = 1, "Waiting for the funding transaction signatures"
        MEMPOOL = 2, "Funding transaction in the mempool"
        LOCKED = 3, "Escrow confirmed"
        RELEASING = 4, "Waiting for the release signatures"
        RELEASED = 5, "Released to the buyer"
        REFUNDED = 6, "Returned to the seller"
        RESCUED = 7, "Recovered by the seller after the timelock"
        ABANDONED = 8, "Never funded"

    class ReleaseKind(models.IntegerChoices):
        COOPERATIVE = 0, "Seller and buyer (key path)"
        BUYER_WINS = 1, "Buyer and coordinator"
        SELLER_WINS = 2, "Seller and coordinator"
        RESCUE = 3, "Seller after the timelock"

    order = models.OneToOneField(
        "api.Order", related_name="onchain_escrow", on_delete=models.CASCADE
    )
    status = models.PositiveSmallIntegerField(
        choices=Status.choices, default=Status.KEYS
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    seller_pubkey = models.CharField(max_length=66, blank=True, default="")
    buyer_pubkey = models.CharField(max_length=66, blank=True, default="")
    coordinator_pubkey = models.CharField(max_length=66, blank=True, default="")
    rescue_csv = models.PositiveIntegerField(default=4480)
    address = models.CharField(max_length=100, blank=True, default="")
    script_pubkey = models.CharField(max_length=68, blank=True, default="")

    escrow_sats = models.PositiveBigIntegerField(default=0)
    coordinator_fee_sats = models.PositiveBigIntegerField(default=0)
    dev_fee_sats = models.PositiveBigIntegerField(default=0)
    funding_tx = models.TextField(blank=True, default="")
    funding_txid = models.CharField(max_length=64, blank=True, default="")
    funding_vout = models.PositiveIntegerField(null=True, blank=True)
    funding_signatures = models.JSONField(default=dict, blank=True)
    confirmed_height = models.PositiveIntegerField(null=True, blank=True)

    release_kind = models.PositiveSmallIntegerField(
        choices=ReleaseKind.choices, null=True, blank=True
    )
    release_tx = models.TextField(blank=True, default="")
    release_txid = models.CharField(max_length=64, blank=True, default="")
    release_nonces = models.JSONField(default=dict, blank=True)
    release_partial_sigs = models.JSONField(default=dict, blank=True)
    release_fee_rate = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"ESCROW-{self.order_id}: {self.get_status_display()}"
