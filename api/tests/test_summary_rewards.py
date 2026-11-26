"""26 nov 2026 (Kilombino TG 15592/15596, order #29): the final summary breaks rewards down by
reason, and an on-chain bond penalized when the dispute opened but credited back to its
winning owner is not shown as lost."""

import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api import order_summary
from api.logics import ledger_credit
from api.models import Currency, LNPayment, OnchainBond, Order


def _user():
    return User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")


def _ln(sender, concept, sats, status=LNPayment.Status.SETLED, payment_hash=None):
    now = timezone.now()
    return LNPayment.objects.create(
        concept=concept, type=LNPayment.Types.HOLD, sender=sender, status=status, num_satoshis=sats,
        payment_hash=payment_hash or uuid.uuid4().hex, preimage=uuid.uuid4().hex,
        invoice=f"lnbcrt{uuid.uuid4().hex}", created_at=now, expires_at=now + timezone.timedelta(hours=3),
    )


class Order29(TestCase):
    """Hybrid B: the maker (buyer) with an on-chain bond won; the seller lost."""

    def setUp(self):
        self.p = [patch("api.logics.send_notification"), patch("api.models.order.send_notification")]
        for x in self.p:
            x.start()
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        now = timezone.now()
        self.buyer, self.seller = _user(), _user()
        self.order = Order.objects.create(
            currency=currency, type=Order.Types.BUY, status=Order.Status.TLD, created_at=now,
            expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100, satoshis=1_000_000,
            t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=self.buyer, taker=self.seller, bond_size=3,
            is_explicit=True, is_disputed=True, taker_lightning=True,
        )
        bond = OnchainBond.objects.create(
            order=self.order, owner=self.buyer, role="maker", owner_pubkey="02" + "11" * 32,
            coordinator_pubkey="02" + "22" * 32, refund_csv=144, address="bcrt1pmaker", script_pubkey="5120" + "33" * 32,
            required_sats=30_000, txid="93" * 32, vout=0, value=30_000, status=OnchainBond.Status.PENALIZED,
            spend_txid="da" * 32,
        )
        self.order.maker_bond = _ln(self.buyer, LNPayment.Concepts.MAKEBOND, 30_000, payment_hash=f"onchain-bond-{bond.pk}")
        self.order.taker_bond = _ln(self.seller, LNPayment.Concepts.TAKEBOND, 30_000)
        self.order.trade_escrow = _ln(self.seller, LNPayment.Concepts.TRESCROW, 1_037_406)
        self.order.save()
        ledger_credit(self.buyer.robot, 1_029_800, "admin", f"admin:{self.order.id}:{self.buyer.robot.pk}:maker_wins",
                      order_id=self.order.id, note="maker_wins")
        ledger_credit(self.seller.robot, 37_406, "swap_surcharge_back", f"swap_back:{self.order.id}",
                      order_id=self.order.id)

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_winner_bond_is_credited_not_lost(self):
        with patch("api.escrow.bonds.confirmed_coins", return_value=[]), patch("api.escrow.bonds.leftovers", return_value=[]):
            s = order_summary.final_summary(self.order, self.buyer)
        self.assertEqual(s["bond_kind"], "onchain")
        self.assertEqual(s["bond_state"], "credited")
        self.assertEqual(s["bond_lost_sats"], 0)
        self.assertEqual(s["bond_credited_sats"], 30_000)
        self.assertEqual(s["rewards"], [{"reason": "trade_amount", "sats": 999_800}, {"reason": "bond_value", "sats": 30_000}])
        self.assertEqual(s["rewards_sats"], 1_029_800)
        self.assertEqual([t["label"] for t in s["txs"]], ["bond", "bond_penalty"])
        self.assertEqual(s["txs"][1]["txid"], "da" * 32)

    def test_loser_reward_is_the_swap_surcharge_refund(self):
        s = order_summary.final_summary(self.order, self.seller)
        self.assertEqual(s["rewards"], [{"reason": "swap_surcharge_refund", "sats": 37_406}])
        self.assertEqual(s["bond_state"], "lost")
        self.assertGreater(s["bond_lost_sats"], 0)

    def test_swap_layer_is_only_the_buyers(self):
        """A Lightning escrow paid out on chain by the swap: the seller sent by Lightning."""
        Order.objects.filter(pk=self.order.pk).update(is_swap=True)
        self.order.refresh_from_db()
        with patch("api.escrow.bonds.confirmed_coins", return_value=[]), patch("api.escrow.bonds.leftovers", return_value=[]):
            self.assertEqual(order_summary.final_summary(self.order, self.seller)["trade_layer"], "lightning")
            self.assertEqual(order_summary.final_summary(self.order, self.buyer)["trade_layer"], "onchain_swap")
