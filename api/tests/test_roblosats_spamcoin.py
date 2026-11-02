"""
Roblosats rl24: the Spamcoin amount of an order is fixed by the coordinator.

- Currency.exchange_rate keeps 8 decimals: the BTCB2→BTC rate (currency 1000) is about
  0.0047, and with 4 decimals it jumped in 2 % steps (0.0047, 0.0048, 0.0049), so two
  apps showed the same order 2 % apart.
- Logics.spamcoin_sats: last_satoshis × that rate, only for orders paid in Spamcoin; an
  order priced in Spamcoin itself is its amount.
"""

from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase

from api.logics import Logics
from api.models import Currency

SPAMCOIN = 1000


def order(method="Spamcoin Lightning", currency=1, amount=10, last_satoshis=2_683_267):
    return SimpleNamespace(
        payment_method=method,
        currency=SimpleNamespace(currency=currency),
        amount=Decimal(amount),
        last_satoshis=last_satoshis,
    )


class SpamcoinSatsTest(TestCase):
    def set_ratio(self, ratio):
        Currency.objects.update_or_create(
            currency=SPAMCOIN, defaults={"exchange_rate": ratio}
        )

    def test_rate_keeps_eight_decimals(self):
        self.set_ratio(0.00474798)
        stored = Currency.objects.get(currency=SPAMCOIN).exchange_rate
        self.assertEqual(stored, Decimal("0.00474798"))

    def test_fixed_from_last_satoshis_and_rate(self):
        self.set_ratio(0.00474798)
        self.assertEqual(Logics.spamcoin_sats(order()), round(2_683_267 * 0.00474798))

    def test_method_match_ignores_case(self):
        self.set_ratio(0.005)
        self.assertEqual(Logics.spamcoin_sats(order(method="spamcoin")), 13_416)

    def test_other_methods_get_none(self):
        self.set_ratio(0.005)
        self.assertIsNone(Logics.spamcoin_sats(order(method="SEPA")))
        self.assertIsNone(Logics.spamcoin_sats(order(method="")))

    def test_no_rate_gives_none(self):
        Currency.objects.filter(currency=SPAMCOIN).delete()
        self.assertIsNone(Logics.spamcoin_sats(order()))

    def test_order_priced_in_spamcoin_is_its_amount(self):
        o = order(currency=SPAMCOIN, amount=Decimal("0.00012880"))
        self.assertEqual(Logics.spamcoin_sats(o), 12_880)
