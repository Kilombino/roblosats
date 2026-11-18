"""Roblosats rl31-beta19: "丰 sat" orders.

The amount (or range) is in sats of BTC; the order's currency is what is paid in exchange.
By default the maker fixes the total it wants in exchange (fixed_price = total / sats, so the
price per sat stays the same along a range); optionally the price is the market price + premium,
frozen in contract_price when the taker's bond locks. Sizes, onchain limits and bonds come from
the sats directly; the summary, ticks and the Nostr event show what is paid in exchange.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from api.escrow import flow
from api.logics import Logics
from api.models import Currency, Order
from api.nostr import Nostr
from api.tests.test_roblosats_fixed_price import FixedPriceApi, FixedPriceBase, _user

SPAMCOIN = 1000


class SatOrderBase(FixedPriceBase):
    def sat_order(self, fixed_price=None, amount=25_000, rng=None, currency=None, **kw):
        o = self._order(fixed_price, None if rng else Decimal(amount), rng=rng and (Decimal(rng[0]), Decimal(rng[1])),
                        sats_denominated=True, **kw)
        if currency is not None:
            o.currency = currency
            o.save()
        return o

    def spamcoin(self, rate=None):
        c, _ = Currency.objects.update_or_create(currency=SPAMCOIN, defaults={"exchange_rate": rate})
        return c


class SatOrderLogics(SatOrderBase):
    def test_sats_are_the_amount(self):
        # 25,000 sat for 50,000 Spamsats: price 2 Spamcoin per BTC
        o = self.sat_order(Decimal("2"), 25_000, currency=self.spamcoin())
        self.assertEqual(Logics.satoshis_now(o), 25_000)
        self.assertEqual(o.t0_satoshis, 25_000)
        self.assertEqual(Logics.exchange_total(o, 25_000), 0.0005)  # 50,000 Spamsats
        self.assertEqual(Logics.price_and_premium_now(o), (2.0, None))

    def test_range_keeps_the_price_per_sat(self):
        o = self.sat_order(Decimal("50000"), rng=(30_000, 90_000))  # 50,000 EUR per BTC
        self.assertEqual(Logics.satoshis_now(o), 90_000)
        self.assertEqual(Logics.satoshis_now(o, Decimal("40000")), 40_000)
        self.assertEqual(Logics.exchange_total(o, 30_000), 15.0)
        self.assertEqual(Logics.exchange_total(o, 90_000), 45.0)

    def test_market_premium_and_frozen_at_take(self):
        self.set_rate(40_000)
        o = self.sat_order(None, 100_000, premium=Decimal("5"))
        self.assertEqual(Logics.satoshis_now(o), 100_000)
        self.assertEqual(Logics.exchange_total(o, 100_000), 42.0)
        self.assertEqual(Logics.price_and_premium_now(o), (42_000.0, 5.0))
        Logics.lock_contract_price(o)
        self.set_rate(80_000)
        o.currency.refresh_from_db()
        self.assertEqual(Logics.exchange_total(o, 100_000), 42.0)  # frozen
        self.assertEqual(Logics.fiat_of(o), 42.0)

    def test_market_without_rate(self):
        o = self.sat_order(None, 100_000, premium=Decimal("5"))
        self.assertIsNone(Logics.exchange_total(o, 100_000))
        self.assertEqual(Logics.satoshis_now(o), 100_000)

    def test_spamcoin_sats_paid_in_exchange(self):
        o = self.sat_order(Decimal("2"), 25_000, currency=self.spamcoin())
        o.payment_method = "Spamcoin"
        self.assertEqual(Logics.spamcoin_sats(o), 50_000)

    def test_sizes_and_onchain_limits_from_sats(self):
        o = self.sat_order(Decimal("50000"), rng=(30_000, 100_000))
        env = {"ESCROW_MIN_SATS": 20000, "ESCROW_MAX_SATS": 101000}
        with patch("api.escrow.flow.config", side_effect=lambda k, cast=None, default=None: env.get(k, default)):
            self.assertIsNone(flow.onchain_size_error(o))
            o.max_amount = Decimal("120000")
            self.assertIn("between 20000 and 101000", flow.onchain_size_error(o))
        with patch("api.logics.MIN_ORDER_SIZE", 20_000), patch("api.logics.MAX_ORDER_SIZE", 1_000_000):
            o.max_amount = Decimal("100000")
            self.assertEqual(Logics.validate_order_size(o), (True, None))

    def test_str(self):
        self.assertIn("25000 sat for", str(self.sat_order(Decimal("2"), 25_000)))


class SatOrderNostr(SatOrderBase):
    def test_single_amount(self):
        o = self.sat_order(Decimal("2"), 25_000, currency=self.spamcoin())
        p = Nostr.pricing_tags(o)
        self.assertEqual(p["amt"], "25000")
        self.assertEqual(p["fa"], ["0.0005"])
        self.assertEqual(p["sats"], ["25000"])
        self.assertEqual(p["price"], "2")

    def test_range_and_generate_tags(self):
        o = self.sat_order(Decimal("50000"), rng=(30_000, 90_000))
        env = {"COORDINATOR_ALIAS": "T", "HOST_NAME": "h", "NETWORK": "mainnet"}
        with patch("api.nostr.config", side_effect=lambda k, cast=None, default="": env.get(k, default)):
            tags = {t.to_vec()[0]: t.to_vec()[1:] for t in Nostr().generate_tags(o, "R", "h", "EUR")}
        self.assertEqual(tags["amt"], ["0"])
        self.assertEqual(tags["fa"], ["15", "45"])
        self.assertEqual(tags["sats"], ["30000", "90000"])
        self.assertEqual(tags["price"], ["50000"])

    def test_market_premium(self):
        self.set_rate(40_000)
        o = self.sat_order(None, 100_000, premium=Decimal("5"))
        p = Nostr.pricing_tags(o)
        self.assertEqual((p["amt"], p["fa"], p["premium"], p["price"]), ("100000", ["42"], "5", None))

    def test_normal_orders_have_no_sats_tag(self):
        o = self._order(Decimal("50000"), Decimal("100"))
        self.assertNotIn("sats", Nostr.pricing_tags(o))


@override_settings(DEFAULT_BOND_SIZE=3)
class SatOrderApi(FixedPriceApi, SatOrderBase):
    def test_make_with_fixed_total(self):
        c = self.spamcoin()
        res = self.make(_user(), currency=c.id, amount="25000", sats_denominated=True, fixed_price="2",
                        payment_method="Spamcoin")
        self.assertEqual(res.status_code, 201, res.data)
        o = Order.objects.get(id=res.data["id"])
        self.assertTrue(o.sats_denominated)
        self.assertEqual((o.t0_satoshis, o.last_satoshis), (25_000, 25_000))
        self.assertTrue(res.data["sats_denominated"])

    def test_make_range_and_limits(self):
        ok = self.make(_user(), amount=None, has_range=True, min_amount="30000", max_amount="90000",
                       sats_denominated=True, fixed_price="50000")
        self.assertEqual(ok.status_code, 201, ok.data)
        low = self.make(_user(), amount=None, has_range=True, min_amount="10000", max_amount="90000",
                        sats_denominated=True, fixed_price="50000")
        self.assertEqual(low.status_code, 400)  # 10,000 < 20,000

    def test_whole_sats_only(self):
        res = self.make(_user(), amount="25000.5", sats_denominated=True, fixed_price="50000")
        self.assertEqual(res.status_code, 400)

    def test_market_needs_a_rate(self):
        self.assertEqual(self.make(_user(), amount="50000", sats_denominated=True, premium=3).status_code, 400)
        self.set_rate(40_000)
        res = self.make(_user(), amount="50000", sats_denominated=True, premium=3)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get(id=res.data["id"]).t0_satoshis, 50_000)

    def test_order_page_says_what_is_paid(self):
        from api.views import OrderView

        o = self.sat_order(Decimal("50000"), rng=(30_000, 90_000))
        from rest_framework.test import APIRequestFactory, force_authenticate

        req = APIRequestFactory().get(f"/api/order/?order_id={o.id}")
        force_authenticate(req, user=_user())
        res = OrderView.as_view({"get": "get"})(req)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["sats_denominated"])
        self.assertEqual((res.data["exchange_min_amount"], res.data["exchange_max_amount"]), ("15", "45"))
        self.assertEqual(res.data["satoshis_now"], 90_000)

    def test_book_shows_what_is_paid_and_the_sats(self):
        from api.views import BookView

        data = BookView._public(self.sat_order(Decimal("50000"), rng=(30_000, 90_000)))
        self.assertEqual((data["min_amount"], data["max_amount"]), ("15", "45"))
        self.assertEqual((data["sats_min_amount"], data["sats_max_amount"]), (30_000, 90_000))
        self.assertTrue(data["sats_denominated"])
        self.assertEqual(data["satoshis_now"], 90_000)

    def test_take_range_in_sats(self):
        o = self.sat_order(Decimal("40000"), rng=(30_000, 90_000))
        self.assertTrue(Logics.validate_amount_within_range(o, 40_000)[0])
        self.assertFalse(Logics.validate_amount_within_range(o, 100)[0])
        taker = _user()
        Logics.take(o, taker, amount=40_000)
        from api.models import TakeOrder

        take = TakeOrder.objects.get(order=o, taker=taker)
        self.assertEqual(Logics.satoshis_now(o, take.amount), 40_000)

    def test_info_announces_it(self):
        from api.views import InfoView
        from rest_framework.test import APIRequestFactory

        res = InfoView.as_view({"get": "get"})(APIRequestFactory().get("/api/info/"))
        self.assertTrue(res.data["sats_orders"])


# --- regtest: the whole onchain trade of a sat order ---------------------------------------
from unittest import skipUnless  # noqa: E402

from api.tests import test_escrow_service_regtest as base  # noqa: E402


@skipUnless(base.REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class SatOrderRegtest(base.EscrowOrderFlowRegtest):
    """A "丰 sat" onchain order paid in Spamsats at a fixed total, through the real bonds,
    deposit and release of the lab node: 1,000,000 sat for 2,000,000 Spamsats (2 Spamcoin per
    BTC), and a range 500,000-1,000,000 sat taken at 1,000,000 (the maker's bond of a range is on
its maximum, as for any range, so the outputs match the flow tests). The outputs are the same as a
    1,000,000-sat order of any other kind (the sats come from the amount, not from a price)."""

    RANGE = False
    # only the happy path and the collaborative cancel of the flow tests
    test_fair_rescue_without_the_coordinator = None
    test_rescue_plan_1_kept_for_old_escrows = None
    test_dispute_for_the_buyer_from_the_admin = None
    test_dispute_return_everything_from_the_admin = None
    test_taker_never_funds_is_penalized = None

    def _make_and_take(self):
        from django.utils import timezone

        from api.escrow import flow

        buyer, seller = base._user(), base._user()
        now = timezone.now()
        spam, _ = Currency.objects.get_or_create(currency=SPAMCOIN, defaults={"exchange_rate": None})
        rng = self.RANGE
        order = Order.objects.create(
            currency=spam, type=Order.Types.BUY, status=Order.Status.WFB, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=rng,
            amount=None if rng else Decimal("1000000"), min_amount=Decimal("500000") if rng else None,
            max_amount=Decimal("1000000") if rng else None, sats_denominated=True, fixed_price=Decimal("2"),
            payment_method="Spamcoin", maker=buyer, bond_size=3,
        )
        order.t0_satoshis = order.last_satoshis = Logics.satoshis_now(order)
        order.save()
        self.assertEqual(order.last_satoshis, 1_000_000)
        kb, ks = self._keys(order, "buyer"), self._keys(order, "seller")
        self._pay_bond(order, buyer, kb)
        self.assertEqual(order.status, Order.Status.PUB)
        if rng:
            self.assertFalse(Logics.validate_amount_within_range(order, 400_000)[0])
            Logics.take(order, seller, amount=1_000_000)
        else:
            Logics.take(order, seller)
        self._pay_bond(order, seller, ks)
        self.assertEqual(order.status, Order.Status.WF2)
        order.refresh_from_db()
        self.assertEqual(order.last_satoshis, 1_000_000)
        self.assertEqual(Logics.spamcoin_sats(order), 2_000_000)
        self.assertEqual(Logics.fiat_of(order), 0.02)
        self.assertIsNotNone(flow.escrow_of(order))
        return order, buyer, seller, {"buyer": kb, "seller": ks}


class SatOrderRangeRegtest(SatOrderRegtest):
    RANGE = True
