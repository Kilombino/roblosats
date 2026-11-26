import uuid

from decimal import Decimal
from decouple import config
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

FEE = float(config("FEE"))


class MarketTick(models.Model):
    """
    Records tick by tick Non-KYC Bitcoin price.
    Data to be aggregated and offered via public API.

    It is checked against current CEX price for useful
    insight on the historical premium of Non-KYC BTC

    Price is set when taker bond is locked. Both
    maker and taker are committed with bonds (contract
    is finished and cancellation has a cost)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # 0.9.15: 8 decimals (a Spamcoin price per BTC is ~0.0036: 2 decimals stored 0.00)
    price = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=None,
        null=True,
        validators=[MinValueValidator(Decimal(0))],
    )
    # 0.9.15: up to 99,999,999 BTC (8 digits allowed at most 0.99999999 BTC: a tick of a 1 BTC or
    # bigger contract failed silently)
    volume = models.DecimalField(
        max_digits=16,
        decimal_places=8,
        default=None,
        null=True,
        validators=[MinValueValidator(Decimal(0))],
    )
    premium = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=None,
        null=True,
        validators=[MinValueValidator(Decimal(-100)), MaxValueValidator(Decimal(999))],
        blank=True,
    )
    currency = models.ForeignKey("api.Currency", null=True, on_delete=models.SET_NULL)
    timestamp = models.DateTimeField(default=timezone.now)

    # Relevant to keep record of the historical fee, so the insight on the premium can be better analyzed
    fee = models.DecimalField(
        max_digits=4,
        decimal_places=4,
        default=0,
        validators=[MinValueValidator(Decimal(0)), MaxValueValidator(Decimal(1))],
    )

    def log_a_tick(order):
        """
        Creates a new tick
        """
        from api.models import LNPayment

        # 0.9.15: every formalized contract is a tick, whatever bond the taker locked. Only
        # Lightning-bonded takes were logged: onchain orders taken with an onchain bond and
        # mixta D (order.taker_bond is None) never got a tick, so they were missing from the
        # 24 h and lifetime volumes.
        if order.taker is None:
            return None
        if order.taker_bond is not None and order.taker_bond.status != LNPayment.Status.LOCKED:
            return None
        volume = order.last_satoshis / 100_000_000
        if order.sats_denominated:  # rl31-beta19: "丰 sat" order
            from api.logics import Logics

            price = Logics.price_per_btc(order) or 0
        else:
            price = float(order.amount) / volume  # Amount Fiat / Amount BTC
        # Roblosats: a fixed-price order may be taken while there is no market price;
        # keep the premium inside the field's 5 digits (a fixed price can be far off).
        market_exchange_rate = float(order.currency.exchange_rate or 0)
        if market_exchange_rate > 0:
            premium = 100 * (price / market_exchange_rate - 1)
            premium = max(-99.99, min(999, round(premium, 2)))
        else:
            premium = None

        market_tick = MarketTick.objects.create(
            price=price,
            volume=volume,
            premium=premium,
            currency=order.currency,
            fee=config("FEE", cast=float, default=0),
        )

        return market_tick

    def __str__(self):
        return f"Tick: {str(self.id)[:8]}"

    class Meta:
        verbose_name = "Market tick"
        verbose_name_plural = "Market ticks"
