"""Roblosats rl31-beta17: FIXED PRICE orders.

The maker sets the price per BTC (Order.fixed_price, in the order's currency: fiat, or
Spamcoin for currency 1000). Sats = amount / fixed_price (a range: with the amount taken).
Nothing about such an order needs the coordinator's exchange rate: sizes, onchain limits,
the book, the order page, ticks and the Nostr event all work with no market price, and the
premium against the market is only informative. Relative orders are unchanged (and cannot
be made while the currency has no market price).
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from api.escrow import flow
from api.logics import Logics
from api.models import Currency, MarketTick, Order
from api.nostr import Nostr
from api.utils import compute_avg_premium

EUR = 2


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


class FixedPriceBase(TestCase):
    def setUp(self):
        self.patches = [
            patch("api.logics.send_notification"),
            patch("api.logics.nostr_send_order_event"),
            patch("api.logics.send_devfund_donation"),
            patch("api.models.order.send_notification"),
        ]
        for p in self.patches:
            p.start()
        User.objects.get_or_create(username="admin")
        self.eur, _ = Currency.objects.update_or_create(
            currency=EUR, defaults={"exchange_rate": None}
        )

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def set_rate(self, rate):
        self.eur.exchange_rate = rate
        self.eur.save()
        self.eur.refresh_from_db()

    def _order(self, fixed_price=Decimal("50000"), amount=Decimal("100"), rng=None, **kw):
        now = timezone.now()
        o = Order(
            currency=self.eur,
            type=kw.pop("type", Order.Types.SELL),
            status=kw.pop("status", Order.Status.PUB),
            created_at=now,
            expires_at=now + timezone.timedelta(hours=24),
            has_range=rng is not None,
            amount=None if rng else amount,
            min_amount=rng[0] if rng else None,
            max_amount=rng[1] if rng else None,
            premium=kw.pop("premium", 0),
            fixed_price=fixed_price,
            maker=kw.pop("maker", None) or _user(),
            payment_method="SEPA",
            **kw,
        )
        o.t0_satoshis = o.last_satoshis = Logics.satoshis_now(o)
        o.save()
        return o


class FixedPriceLogics(FixedPriceBase):
    def test_sats_from_fixed_price_without_rate(self):
        o = self._order(Decimal("50000"), Decimal("100"))
        self.assertEqual(Logics.satoshis_now(o), 200_000)  # 100 / 50,000 EUR per BTC
        self.assertEqual(o.t0_satoshis, 200_000)

    def test_range_sats_at_the_amount_taken(self):
        o = self._order(Decimal("40000"), rng=(Decimal("40"), Decimal("200")))
        self.assertEqual(Logics.satoshis_now(o), 500_000)  # max of the range
        self.assertEqual(Logics.satoshis_now(o, Decimal("60")), 150_000)

    def test_rate_does_not_change_the_sats(self):
        o = self._order(Decimal("50000"), Decimal("100"))
        for rate in (None, 0, 25_000, 100_000):
            self.set_rate(rate)
            o.currency.refresh_from_db()
            self.assertEqual(Logics.satoshis_now(o), 200_000)

    def test_price_and_informative_premium(self):
        o = self._order(Decimal("55000"))
        self.assertEqual(Logics.price_and_premium_now(o), (55000.0, None))
        self.set_rate(50_000)
        o.currency.refresh_from_db()
        self.assertEqual(Logics.price_and_premium_now(o), (55000.0, 10.0))

    def test_premium_kept_inside_the_field(self):
        o = self._order(Decimal("5000000"))
        self.set_rate(1_000)
        o.currency.refresh_from_db()
        self.assertEqual(Logics.fixed_price_premium(o), 999)
        o.fixed_price = Decimal("0.01")
        self.assertEqual(Logics.fixed_price_premium(o), -99.99)

    def test_relative_orders_unchanged_and_safe_without_rate(self):
        self.set_rate(50_000)
        o = self._order(None, Decimal("100"), premium=Decimal("5"))
        self.assertEqual(Logics.satoshis_now(o), int(100 / 52_500 * 1e8))
        self.assertEqual(Logics.price_and_premium_now(o), (52500.0, Decimal("5")))
        self.set_rate(None)
        o.currency.refresh_from_db()
        self.assertEqual(Logics.satoshis_now(o), 0)
        self.assertEqual(Logics.price_and_premium_now(o)[0], 0)

    def test_order_size_from_fixed_price(self):
        o = self._order(Decimal("50000"), rng=(Decimal("60"), Decimal("200")))
        with patch("api.logics.MIN_ORDER_SIZE", 100_000), patch("api.logics.MAX_ORDER_SIZE", 500_000):
            self.assertEqual(Logics.validate_order_size(o), (True, None))  # 120k..400k
            o.min_amount = Decimal("40")  # 80k sats
            ok, err = Logics.validate_order_size(o)
            self.assertFalse(ok)
            self.assertEqual(err["bad_request"].count("80000"), 1)

    def test_onchain_limits_from_fixed_price(self):
        o = self._order(Decimal("50000"), rng=(Decimal("15"), Decimal("50")))  # 30k..100k
        env = {"ESCROW_MIN_SATS": 20000, "ESCROW_MAX_SATS": 101000}
        with patch("api.escrow.flow.config", side_effect=lambda k, cast=None, default=None: env.get(k, default)):
            self.assertIsNone(flow.onchain_size_error(o))
            o.max_amount = Decimal("60")  # 120k
            self.assertIn("between 20000 and 101000", flow.onchain_size_error(o))

    def test_tick_without_rate(self):
        o = self._order(Decimal("50000"), Decimal("100"))
        self.assertIsNone(MarketTick.objects.filter(currency=self.eur).first())
        tick = MarketTick.objects.create(price=50000, volume=Decimal("0.002"), premium=None, currency=self.eur)
        self.assertEqual(compute_avg_premium(MarketTick.objects.filter(id=tick.id)), (0.0, 0.002))
        self.assertTrue(Logics.is_fixed_price(o))


class FixedPriceNostr(FixedPriceBase):
    def test_tags_single_amount(self):
        o = self._order(Decimal("50000.00"), Decimal("100"))
        self.assertEqual(Nostr.pricing_tags(o), {"amt": "200000", "premium": "0", "price": "50000"})
        self.set_rate(40_000)
        o.currency.refresh_from_db()
        self.assertEqual(Nostr.pricing_tags(o)["premium"], "25.0")

    def test_tags_range(self):
        o = self._order(Decimal("40000"), rng=(Decimal("40"), Decimal("200")))
        self.assertEqual(Nostr.pricing_tags(o)["amt"], "0")
        self.assertEqual(Nostr.pricing_tags(o)["price"], "40000")

    def test_relative_tags_unchanged(self):
        self.set_rate(50_000)
        o = self._order(None, Decimal("100"), premium=Decimal("1.5"))
        self.assertEqual(Nostr.pricing_tags(o), {"amt": "0", "premium": "1.5", "price": None})

    def test_generate_tags_carries_price(self):
        o = self._order(Decimal("50000"), Decimal("100"))
        env = {"COORDINATOR_ALIAS": "T", "HOST_NAME": "h", "NETWORK": "mainnet"}
        with patch("api.nostr.config", side_effect=lambda k, cast=None, default="": env.get(k, default)):
            tags = {t.to_vec()[0]: t.to_vec()[1:] for t in Nostr().generate_tags(o, "R", "h", "EUR")}
        self.assertEqual(tags["amt"], ["200000"])
        self.assertEqual(tags["price"], ["50000"])


@override_settings(DEFAULT_BOND_SIZE=3)
class FixedPriceApi(FixedPriceBase):
    def make(self, user, **body):
        from api.views import MakerView

        data = {
            "type": Order.Types.SELL,
            "currency": self.eur.id,
            "amount": "100",
            "payment_method": "SEPA",
            "premium": 0,
            "public_duration": 3600,
            "escrow_duration": 3600,
            "bond_size": 3,
        }
        data.update(body)
        req = APIRequestFactory().post("/api/make/", data, format="json")
        force_authenticate(req, user=user)
        env = {"MAX_PUBLIC_ORDERS": "100"}
        with patch("api.views.config", side_effect=lambda k, cast=None, default=None: env.get(k, default)), \
                patch("api.logics.MIN_ORDER_SIZE", 20_000), patch("api.logics.MAX_ORDER_SIZE", 1_000_000):
            return MakerView.as_view()(req)

    def test_make_fixed_price_without_rate(self):
        res = self.make(_user(), fixed_price="50000")
        self.assertEqual(res.status_code, 201, res.data)
        o = Order.objects.get(id=res.data["id"])
        self.assertEqual(o.t0_satoshis, 200_000)
        self.assertEqual(o.premium, 0)
        self.assertFalse(o.is_explicit)
        self.assertEqual(Decimal(res.data["fixed_price"]), Decimal("50000"))

    def test_make_fixed_range_without_rate(self):
        res = self.make(_user(), amount=None, has_range=True, min_amount="20", max_amount="150", fixed_price="50000")
        self.assertEqual(res.status_code, 201, res.data)
        res = self.make(_user(), amount=None, has_range=True, min_amount="5", max_amount="150", fixed_price="50000")
        self.assertEqual(res.status_code, 400)  # 10,000 sats < 20,000

    def test_make_fixed_price_stores_informative_premium(self):
        self.set_rate(40_000)
        res = self.make(_user(), fixed_price="50000")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get(id=res.data["id"]).premium, Decimal("25"))

    def test_relative_without_rate_is_refused(self):
        res = self.make(_user(), premium=2)
        self.assertEqual(res.status_code, 400)
        self.assertIn("fixed price", res.data["bad_request"])

    def test_bad_fixed_price(self):
        self.assertEqual(self.make(_user(), fixed_price="0").status_code, 400)
        self.assertEqual(self.make(_user(), fixed_price="-5").status_code, 400)

    def test_relative_with_rate_unchanged(self):
        self.set_rate(50_000)
        res = self.make(_user(), premium=0)
        self.assertEqual(res.status_code, 201, res.data)
        o = Order.objects.get(id=res.data["id"])
        self.assertIsNone(o.fixed_price)
        self.assertEqual(o.t0_satoshis, 200_000)

    def test_book_and_order_page_without_rate(self):
        from api.views import BookView, OrderView

        maker = _user()
        o = self._order(Decimal("50000"), Decimal("100"), maker=maker)
        data = BookView._public(o)
        self.assertEqual(data["premium"], "0")
        self.assertEqual(data["price"], 50000.0)
        self.assertEqual(data["satoshis_now"], 200_000)
        self.assertEqual(Decimal(data["fixed_price"]), Decimal("50000"))
        req = APIRequestFactory().get(f"/api/order/?order_id={o.id}")
        force_authenticate(req, user=_user())
        res = OrderView.as_view({"get": "get"})(req)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["price_now"], 50000.0)
        self.assertIsNone(res.data["premium_now"])
        self.assertEqual(res.data["satoshis_now"], 200_000)

    def test_take_range_at_chosen_amount(self):
        o = self._order(Decimal("40000"), rng=(Decimal("40"), Decimal("200")))
        taker = _user()
        ok, err = Logics.validate_amount_within_range(o, 60)
        self.assertTrue(ok)
        Logics.take(o, taker, amount=60)
        from api.models import TakeOrder

        take = TakeOrder.objects.get(order=o, taker=taker)
        self.assertEqual(Logics.satoshis_now(o, take.amount), 150_000)
