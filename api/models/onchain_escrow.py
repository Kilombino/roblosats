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


class OnchainBond(models.Model):
    class Status(models.IntegerChoices):
        FUNDING = 0, "Waiting for the bond on chain"
        SIGN = 1, "Waiting for the owner's penalty signature"
        LOCKED = 2, "Bond locked"
        IN_DEPOSIT = 3, "Moved into the escrow deposit"
        PENALIZED = 4, "Penalty broadcast"
        RETURNABLE = 5, "Free to return to its owner"
        RETURNED = 6, "Returned to its owner"
        ABANDONED = 7, "Never funded"

    order = models.ForeignKey(
        "api.Order", related_name="onchain_bonds", on_delete=models.CASCADE
    )
    take_order = models.ForeignKey(
        "api.TakeOrder",
        related_name="onchain_bonds",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    owner = models.ForeignKey(
        "auth.User", related_name="onchain_bonds", on_delete=models.CASCADE
    )
    role = models.CharField(max_length=5)
    status = models.PositiveSmallIntegerField(
        choices=Status.choices, default=Status.FUNDING
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    owner_pubkey = models.CharField(max_length=66)
    coordinator_pubkey = models.CharField(max_length=66)
    refund_csv = models.PositiveIntegerField()
    address = models.CharField(max_length=100)
    script_pubkey = models.CharField(max_length=68)
    required_sats = models.PositiveBigIntegerField()

    txid = models.CharField(max_length=64, blank=True, default="")
    vout = models.PositiveIntegerField(null=True, blank=True)
    value = models.PositiveBigIntegerField(default=0)
    penalty_tx = models.TextField(blank=True, default="")
    penalty_owner_sig = models.CharField(max_length=130, blank=True, default="")
    spend_txid = models.CharField(max_length=64, blank=True, default="")
    return_tx = models.TextField(blank=True, default="")
    # rl31-beta18: a bond made of several coins (topped up), the penalty signed per coin, what a
    # return spent, and coins at the bond address that were not the bond, sent back
    coins = models.JSONField(default=list, blank=True)
    penalty_owner_sigs = models.JSONField(default=list, blank=True)
    return_coins = models.JSONField(default=list, blank=True)
    leftover_tx = models.TextField(blank=True, default="")
    leftover_coins = models.JSONField(default=list, blank=True)
    leftover_txids = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"BOND-{self.order_id}-{self.role}: {self.get_status_display()}"
