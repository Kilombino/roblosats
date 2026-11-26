"""26 nov 2026 (Kilombino TG 15590): the dispute page says, for every amount of a Lightning-order
plan, whether it is Lightning or on chain, and what really happens to each bond (a Lightning
hold invoice, or an on-chain bond behind a proxy: hybrid B maker / mixta C taker)."""

import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from django.utils import translation

from api.models import Currency, LNPayment, OnchainBond, Order
from roblosats_admin import disputes


def _user():
    return User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")


def _ln(sender, concept, sats, status=LNPayment.Status.SETLED, payment_hash=None):
    now = timezone.now()
    return LNPayment.objects.create(
        concept=concept, type=LNPayment.Types.HOLD, sender=sender, status=status, num_satoshis=sats,
        payment_hash=payment_hash or uuid.uuid4().hex, preimage=uuid.uuid4().hex,
        invoice=f"lnbcrt{uuid.uuid4().hex}", created_at=now, expires_at=now + timezone.timedelta(hours=3),
    )


class PlanLayers(TestCase):
    def setUp(self):
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        now = timezone.now()
        self.seller, self.buyer = _user(), _user()
        # a SELL order: the maker sells, the taker buys
        self.order = Order.objects.create(
            currency=currency, type=Order.Types.SELL, status=Order.Status.WFR, created_at=now,
            expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100, satoshis=1_000_000,
            t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=self.seller, taker=self.buyer, bond_size=3,
            is_explicit=True, is_disputed=True,
        )
        self.order.maker_bond = _ln(self.seller, LNPayment.Concepts.MAKEBOND, 30_000)
        self.order.taker_bond = _ln(self.buyer, LNPayment.Concepts.TAKEBOND, 30_000)
        self.order.trade_escrow = _ln(self.seller, LNPayment.Concepts.TRESCROW, 1_000_000)
        self.order.payout = _ln(self.buyer, LNPayment.Concepts.PAYBUYER, 998_000, status=LNPayment.Status.VALIDI)
        self.order.save()

    def _text(self, decision):
        with translation.override("en"):
            p = disputes.plan(self.order, decision)
        self.assertTrue(p["possible"], p["missing"])
        return " ".join(p["lines"])

    def _onchain_taker_bond(self, status):
        bond = OnchainBond.objects.create(
            order=self.order, owner=self.buyer, role="taker", owner_pubkey="02" + "11" * 32,
            coordinator_pubkey="02" + "22" * 32, refund_csv=144, address="bcrt1ptaker", script_pubkey="5120" + "33" * 32,
            required_sats=30_000, txid="aa" * 32, vout=0, value=30_000, status=status,
        )
        self.order.taker_bond = _ln(self.buyer, LNPayment.Concepts.TAKEBOND, 30_000, status=LNPayment.Status.LOCKED,
                                    payment_hash=f"onchain-bond-{bond.pk}")
        self.order.save()
        return bond

    def test_lightning_order_says_lightning_for_every_amount(self):
        t = self._text("buyer")
        self.assertIn("the trade amount 998 000 from the Lightning escrow, credited as a Lightning reward", t)
        self.assertIn("their Lightning bond (30 000), credited as a Lightning reward", t)
        self.assertIn("In total 1 043 000 sats credited as a Lightning reward", t)
        self.assertIn("they lose their Lightning bond (30 000; 15 000 go to the buyer as the winner's share and 15 000 to the coordinator) and their Lightning escrow", t)
        t = self._text("seller")
        self.assertIn("their Lightning escrow back (1 000 000), credited as a Lightning reward", t)
        t = self._text("completed")
        self.assertIn("right now over Lightning", t)

    def test_mixta_c_onchain_bond_still_locked(self):
        self._onchain_taker_bond(OnchainBond.Status.LOCKED)
        t = self._text("buyer")
        self.assertIn("their on-chain bond (30 000), straight back to them on chain (not credited)", t)
        self.assertIn("In total 1 013 000 sats credited as a Lightning reward", t)
        t = self._text("seller")
        self.assertIn("they lose their on-chain bond (30 000), penalized on chain now", t)
        t = self._text("return")
        self.assertIn("straight back to them on chain", t)

    def test_onchain_bond_penalized_earlier_is_credited_by_value(self):
        self._onchain_taker_bond(OnchainBond.Status.PENALIZED)
        t = self._text("buyer")
        self.assertIn("the value of their on-chain bond (30 000), penalized on chain earlier, credited as a Lightning reward", t)
        self.assertIn("In total 1 043 000 sats", t)  # 0.9.16: + 15,000 of the seller's bond

    def test_spanish(self):
        with translation.override("es"):
            p = disputes.plan(self.order, "buyer")
        t = " ".join(p["lines"])
        self.assertIn("escrow Lightning", t)
        self.assertIn("recompensa Lightning", t)


class MixtaCAdminActions(PlanLayers):
    """The admin actions credit an on-chain taker bond only by dispute_bond_sats (26 nov 2026:
    "return everything" and "successful trade" credited its value while it also went back on chain)."""

    def _run(self, action):
        from unittest.mock import MagicMock

        from django.contrib.admin.sites import AdminSite

        from api.admin import OrderAdmin

        admin = OrderAdmin(Order, AdminSite())
        admin.message_user = MagicMock()
        with patch("api.admin._credit") as credit, patch("api.admin.send_notification"), \
                patch("api.escrow.bonds.release", return_value=True), patch("api.logics.Logics.pay_buyer", return_value=True), \
                patch("api.models.order.send_notification"):
            getattr(admin, action)(MagicMock(), Order.objects.filter(pk=self.order.pk))
        return [(c.args[0], c.args[1], c.args[3]) for c in credit.call_args_list]

    def test_return_everything_does_not_credit_the_locked_onchain_taker_bond(self):
        self._onchain_taker_bond(OnchainBond.Status.LOCKED)
        credited = self._run("return_everything")
        kinds = [k for _r, _s, k in credited]
        self.assertNotIn("taker_bond", kinds)
        self.assertIn("maker_bond", kinds)

    def test_successful_trade_does_not_credit_the_locked_onchain_taker_bond(self):
        self._onchain_taker_bond(OnchainBond.Status.LOCKED)
        credited = self._run("successful_trade")
        self.assertNotIn("taker_bond", [k for _r, _s, k in credited])

    def test_lightning_taker_bond_still_credited(self):
        credited = self._run("return_everything")
        self.assertIn("taker_bond", [k for _r, _s, k in credited])
