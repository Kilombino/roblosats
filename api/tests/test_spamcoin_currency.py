"""Roblosats 0.9.7: a "丰 sat" order paid only with Spamcoin methods is in Spamcoin (1000).

Kilobot #25 (18 nov 2026): an app made "Spamcoin Lightning" at a 15% premium with currency
EUR, because its currency selector did not follow the method. The coordinator now normalizes
a relative-price sat order to currency 1000 (a premium means the same in any currency) and
refuses a fixed price given in another currency. Orders with an amount in fiat paid through
Spamcoin (spamcoin_sats) are unchanged.
"""

from decimal import Decimal

from api.models import Currency, Order
from api.views import spamcoin_only

from .test_roblosats_fixed_price import FixedPriceApi, _user

SPAM = 1000


class SpamcoinOnly(FixedPriceApi):
    test_make_fixed_price_without_rate = None
    test_make_fixed_range_without_rate = None
    test_make_fixed_price_stores_informative_premium = None
    test_relative_without_rate_is_refused = None
    test_bad_fixed_price = None
    test_relative_with_rate_unchanged = None
    test_book_and_order_page_without_rate = None
    test_take_range_at_chosen_amount = None

    def setUp(self):
        super().setUp()
        self.spam, _ = Currency.objects.update_or_create(currency=SPAM, defaults={"exchange_rate": Decimal("0.0034")})
        self.set_rate(195)

    def test_detects_spamcoin_only_methods(self):
        self.assertTrue(spamcoin_only("Spamcoin Lightning"))
        self.assertTrue(spamcoin_only("Spamcoin Lightning Spamcoin On-Chain"))
        self.assertFalse(spamcoin_only("Spamcoin Lightning SEPA"))
        self.assertFalse(spamcoin_only("SEPA"))
        self.assertFalse(spamcoin_only(""))
        self.assertFalse(spamcoin_only(None))

    def test_relative_sat_order_in_eur_becomes_spamcoin(self):
        res = self.make(_user(), amount="100000", sats_denominated=True, payment_method="Spamcoin Lightning", premium=15)
        self.assertEqual(res.status_code, 201, res.data)
        o = Order.objects.get(id=res.data["id"])
        self.assertEqual(o.currency.currency, SPAM)
        self.assertEqual(o.premium, Decimal("15"))
        self.assertTrue(o.sats_denominated)

    def test_fixed_sat_order_in_eur_is_refused(self):
        res = self.make(_user(), amount="100000", sats_denominated=True, payment_method="Spamcoin Lightning", fixed_price="50000")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Spamcoin", res.data["bad_request"])

    def test_fixed_sat_order_in_spamcoin_is_fine(self):
        res = self.make(_user(), currency=self.spam.id, amount="100000", sats_denominated=True,
                        payment_method="Spamcoin Lightning", fixed_price="0.021")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get(id=res.data["id"]).currency.currency, SPAM)

    def test_mixed_methods_and_fiat_amount_orders_unchanged(self):
        res = self.make(_user(), amount="100000", sats_denominated=True, payment_method="Spamcoin Lightning SEPA", premium=5)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get(id=res.data["id"]).currency.currency, 2)
        # an amount in EUR paid through Spamcoin (spamcoin_sats) keeps EUR
        res = self.make(_user(), amount="1", payment_method="Spamcoin Lightning", premium=5)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Order.objects.get(id=res.data["id"]).currency.currency, 2)
