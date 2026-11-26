"""Roblosats 0.9.15 (Kilombino TG 15665): the "24h contracted volume" stayed at 0 although #34
(Spamcoin, ~0.96 BTC) and #38 (mixta D) completed that day. Two causes: the 24 h sum skipped
currency 1000 (upstream's swaps, Spamcoin in Roblosats), and contracts taken with an onchain bond
(order.taker_bond is None) never logged a market tick. Also: a tick of 1 BTC or more did not fit
the volume column, and a Spamcoin price per BTC was stored with 2 decimals."""

import uuid
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from api.models import Currency, LNPayment, MarketTick, Order
from api.utils import compute_avg_premium


def _user():
    return User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")


class MarketTicks0915(TestCase):
    def setUp(self):
        self.p = [patch("api.logics.send_notification"), patch("api.models.order.send_notification")]
        for x in self.p:
            x.start()
        self.spam, _ = Currency.objects.get_or_create(currency=1000, defaults={"exchange_rate": 0.0036})
        Currency.objects.filter(pk=self.spam.pk).update(exchange_rate=0.0036)
        self.spam.refresh_from_db()
        cache.clear()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _order(self, sats, amount, taker_bond=False, status=Order.Status.SUC):
        now = timezone.now()
        maker, taker = _user(), _user()
        o = Order.objects.create(
            currency=self.spam, type=Order.Types.SELL, status=status, created_at=now,
            expires_at=now + timezone.timedelta(hours=1), has_range=False, amount=amount,
            satoshis=sats, t0_satoshis=sats, last_satoshis=sats, maker=maker, taker=taker,
            bond_size=3, is_explicit=True, contract_finalization_time=now,
        )
        if taker_bond:
            o.taker_bond = LNPayment.objects.create(
                concept=LNPayment.Concepts.TAKEBOND, type=LNPayment.Types.HOLD, sender=taker,
                status=LNPayment.Status.LOCKED, num_satoshis=1000, payment_hash=uuid.uuid4().hex,
                preimage=uuid.uuid4().hex, invoice=f"lnbcrt{uuid.uuid4().hex}", created_at=now, expires_at=now,
            )
            o.save()
        return o

    def test_onchain_bond_contract_logs_a_tick(self):
        o = self._order(20_504, Decimal("0.00000075"))  # mixta D / onchain take: no LN taker bond
        tick = MarketTick.log_a_tick(o)
        self.assertIsNotNone(tick)
        tick.refresh_from_db()
        self.assertEqual(tick.volume, Decimal("0.00020504"))

    def test_big_contract_and_spamcoin_price_fit(self):
        o = self._order(96_080_276 * 2, Decimal("0.00700000"), taker_bond=True)  # 1.92 BTC
        tick = MarketTick.log_a_tick(o)
        tick.refresh_from_db()
        self.assertEqual(tick.volume, Decimal("1.92160552"))
        self.assertGreater(tick.price, Decimal("0.003"))  # 2 decimals stored 0.00

    def test_spamcoin_counts_in_the_24h_volume(self):
        for sats in (96_080_276, 1_000_000):
            MarketTick.log_a_tick(self._order(sats, Decimal("0.0035"), taker_bond=True))
        _, volume = compute_avg_premium(MarketTick.objects.all())
        self.assertAlmostEqual(volume, 0.97080276, places=8)

    def test_info_reports_it(self):
        from rest_framework.test import APIClient

        MarketTick.log_a_tick(self._order(96_080_276, Decimal("0.0035"), taker_bond=True))
        r = APIClient().get("/api/info/")
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(float(r.data["last_day_volume"]), 0.96080276, places=8)

    def test_backfill_only_trades_without_a_tick(self):
        self._order(20_504, Decimal("0.00000075"))  # onchain bond: never logged
        self._order(1_000_000, Decimal("0.0035"), taker_bond=True)  # Lightning bond: logged then
        out = StringIO()
        call_command("backfill_market_ticks", stdout=out)
        self.assertIn("1 trade(s)", out.getvalue())
        self.assertEqual(MarketTick.objects.count(), 0)  # dry run
        call_command("backfill_market_ticks", "--apply", stdout=StringIO())
        self.assertEqual(MarketTick.objects.count(), 1)
