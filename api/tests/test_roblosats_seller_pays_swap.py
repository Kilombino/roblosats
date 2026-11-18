"""Roblosats rl31-beta21: in hybrid B the Lightning seller pays the buyer's swap to onchain.

Mocked unit tests (no node): the surcharge priced once on top of the seller's escrow hold
invoice, the buyer's onchain payout with no swap fee, a mining fee above the priced one, the
unused part back to the seller as rewards (all of it when the buyer takes Lightning or wins a
dispute), the coordinator's proceeds, and the cases with no surcharge (case A, pure Lightning
order, swaps off or out of range, escrow made before).
"""

import math
import uuid
from unittest.mock import patch

from django.utils import timezone

from api import hybrid_swap
from api.logics import Logics
from api.models import LNPayment, OnchainPayment, Order
from api.tests.test_roblosats_hybrid import HybridBase

CFG = {
    "DISABLE_ONCHAIN": False,
    "MIN_SWAP_AMOUNT": 101_000,
    "MAX_SWAP_AMOUNT": 6_000_000,
    "ESCROW_MIN_SATS": 20_000,
}
RATES = (2.0, 3.0)  # swap fee %, suggested sat/vB


def cfg(over=None):
    values = {**CFG, **(over or {})}
    return lambda key, **kw: values.get(key, kw.get("default"))


class SellerPaysSwap(HybridBase):
    def setUp(self):
        super().setUp()
        self.p_cfg = patch("api.hybrid_swap.config", side_effect=cfg())
        self.p_rates = patch("api.hybrid_swap.current_rates", return_value=RATES)
        self.p_cfg.start()
        self.p_rates.start()

    def tearDown(self):
        self.p_cfg.stop()
        self.p_rates.stop()
        super().tearDown()

    def _hybrid_b(self):
        return self._ln_take(self._order(maker_sells=False))

    def _expected(self, order):
        payout = hybrid_swap.buyer_payout_sats(order)
        return payout, int(math.ceil(payout * 2.0 * 1.1 / 100 + math.ceil(3.0 * 280 * 1.5)))

    def _escrow(self, order):
        with patch("api.logics.LNNode") as ln:
            ln.gen_hold_invoice.side_effect = lambda sats, *a, **k: {
                "invoice": f"lnbcrt{uuid.uuid4().hex}",
                "preimage": uuid.uuid4().hex,
                "payment_hash": uuid.uuid4().hex,
                "created_at": timezone.now(),
                "expires_at": timezone.now() + timezone.timedelta(hours=1),
                "cltv_expiry": 100,
            }
            ok, ctx = Logics.gen_escrow_hold_invoice(order, self.taker)
        self.assertTrue(ok, ctx)
        order.refresh_from_db()
        return ctx

    def _tx(self, num, mining_rate=3.0, rate=2.0):
        return OnchainPayment.objects.create(
            receiver=self.maker, balance=None, num_satoshis=num, swap_fee_rate=rate,
            suggested_mining_fee_rate=3, mining_fee_rate=mining_rate,
            mining_fee_sats=int(mining_rate * 280),
        )

    def test_the_seller_escrow_carries_the_swap(self):
        order = self._hybrid_b()
        payout, surcharge = self._expected(order)
        base = Logics.escrow_amount(order, self.taker)[1]["escrow_amount"]
        ctx = self._escrow(order)
        self.assertEqual(order.swap_surcharge_sats, surcharge)
        self.assertEqual(ctx["escrow_satoshis"], base + surcharge)
        self.assertEqual(order.trade_escrow.num_satoshis, base + surcharge)
        # priced once: a second call returns the same invoice and amount
        with patch("api.hybrid_swap.current_rates", return_value=(9.0, 50.0)):
            again = Logics.gen_escrow_hold_invoice(order, self.taker)[1]
        self.assertEqual(again["escrow_satoshis"], base + surcharge)
        # the buyer's own payout is unchanged (its coordinator-fee share only)
        self.assertEqual(payout, round(order.last_satoshis - order.last_satoshis * hybrid_swap._fee_split()[0] * hybrid_swap._fee_split()[1]))

    def test_buyer_gets_the_full_payout_onchain(self):
        order = self._hybrid_b()
        payout, surcharge = self._expected(order)
        self._escrow(order)
        tx = self._tx(payout)
        self.assertLessEqual(hybrid_swap.swap_cost(tx), surcharge)
        self.assertEqual(hybrid_swap.sent_satoshis(order, tx), payout)

    def test_a_mining_fee_above_the_priced_one_comes_out_of_the_buyer(self):
        order = self._hybrid_b()
        payout, surcharge = self._expected(order)
        self._escrow(order)
        tx = self._tx(payout, mining_rate=20.0)
        extra = hybrid_swap.swap_cost(tx) - surcharge
        self.assertGreater(extra, 0)
        self.assertEqual(hybrid_swap.sent_satoshis(order, tx), payout - extra)
        order.is_swap = True
        order.payout_tx = tx
        self.assertEqual(hybrid_swap.refund_due(order), 0)

    def _complete(self, order, is_swap, tx=None):
        order.is_swap = is_swap
        order.payout_tx = tx
        order.save(update_fields=["is_swap", "payout_tx"])
        with patch("api.logics.ledger_credit", return_value=True) as credit:
            refund = hybrid_swap.refund_seller(order)
        return refund, credit

    def test_unused_swap_goes_back_to_the_seller(self):
        order = self._hybrid_b()
        payout, surcharge = self._expected(order)
        self._escrow(order)
        tx = self._tx(payout)
        refund, credit = self._complete(order, True, tx)
        self.assertEqual(refund, surcharge - hybrid_swap.swap_cost(tx))
        robot, sats, kind, fingerprint = credit.call_args.args[:4]
        self.assertEqual((robot, sats, kind), (self.taker.robot, refund, "swap_surcharge_back"))
        self.assertEqual(fingerprint, f"swap_surcharge_back:{order.id}")
        self.assertEqual(order.swap_surcharge_refund_sats, refund)

    def test_buyer_on_lightning_returns_all_of_it(self):
        order = self._hybrid_b()
        _, surcharge = self._expected(order)
        self._escrow(order)
        refund, credit = self._complete(order, False)
        self.assertEqual(refund, surcharge)
        self.assertEqual(credit.call_args.args[1], surcharge)

    def test_proceeds_do_not_keep_the_refund(self):
        order = self._hybrid_b()
        payout, surcharge = self._expected(order)
        self._escrow(order)
        now = timezone.now()
        order.payout = LNPayment.objects.create(
            concept=LNPayment.Concepts.PAYBUYER, type=LNPayment.Types.NORM, sender=self.maker,
            status=LNPayment.Status.SUCCED, num_satoshis=payout, fee=0,
            payment_hash=uuid.uuid4().hex, invoice=f"lnbcrt{uuid.uuid4().hex}",
            created_at=now, expires_at=now + timezone.timedelta(hours=1),
        )
        order.swap_surcharge_refund_sats = surcharge
        order.is_swap = False
        order.save()
        with patch("api.logics.send_devfund_donation"):
            Logics.compute_proceeds(order)
        fee_seller = order.trade_escrow.num_satoshis - surcharge - order.last_satoshis
        self.assertEqual(order.proceeds, fee_seller + (order.last_satoshis - payout))

    def test_dispute_won_by_the_buyer_returns_it(self):
        order = self._hybrid_b()
        _, surcharge = self._expected(order)
        self._escrow(order)
        with patch("api.logics.ledger_credit", return_value=True) as credit:
            self.assertEqual(hybrid_swap.refund_all_to_seller(order, "x"), surcharge)
        self.assertEqual(credit.call_args.args[0], self.taker.robot)

    def test_no_surcharge_in_case_a(self):
        order = self._ln_take(self._order(maker_sells=True))
        self.assertFalse(hybrid_swap.applies(order))
        self.assertEqual(hybrid_swap.fix_surcharge(order), 0)

    def test_no_surcharge_when_swaps_are_off_or_out_of_range(self):
        order = self._hybrid_b()
        with patch("api.hybrid_swap.config", side_effect=cfg({"DISABLE_ONCHAIN": True})):
            self.assertEqual(hybrid_swap.fix_surcharge(order), 0)
        with patch("api.hybrid_swap.config", side_effect=cfg({"MAX_SWAP_AMOUNT": 50_000})):
            self.assertEqual(hybrid_swap.fix_surcharge(order), 0)
        order.refresh_from_db()
        self.assertEqual(order.swap_surcharge_sats, 0)

    def test_no_surcharge_on_a_pure_lightning_order(self):
        order = self._order(maker_sells=False)
        order.escrow_type = Order.EscrowTypes.LIGHTNING
        order.taker = self.taker
        order.save()
        self.assertFalse(hybrid_swap.applies(order))

    def test_without_surcharge_the_upstream_swap_rule_stays(self):
        order = self._hybrid_b()
        order.swap_surcharge_sats = 0  # escrow made before rl31-beta21 (Kilobot #23)
        tx = self._tx(100_000)
        self.assertEqual(hybrid_swap.covered_by_seller(order, tx), 0)
        self.assertEqual(hybrid_swap.refund_due(order), 0)

    def test_take_quote(self):
        from django.core.cache import cache

        cache.delete("hybrid_swap_rates")
        order = self._order(maker_sells=False)
        q = hybrid_swap.take_quote(order)
        self.assertEqual(q["swap_fee_percent"], 2.2)
        self.assertEqual(q["mining_fee_sats"], 1260)
        self.assertEqual(q["min_payout_sats"], 20_000)
        with patch("api.hybrid_swap.config", side_effect=cfg({"DISABLE_ONCHAIN": True})):
            self.assertIsNone(hybrid_swap.take_quote(order))


class SellerPaysSwapViews(SellerPaysSwap):
    def _get(self, order, user):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from api.views import OrderView

        req = APIRequestFactory().get(f"/api/order/?order_id={order.id}")
        force_authenticate(req, user=user)
        return OrderView.as_view({"get": "get"})(req)

    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_take_screen_shows_the_swap_quote(self, _allowed):
        order = self._order(maker_sells=False)
        r = self._get(order, self.taker)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["lightning_take_swap"]["swap_fee_percent"], 2.2)
        # never to the maker, never on an onchain SELL (the taker would be the buyer)
        self.assertNotIn("lightning_take_swap", self._get(order, self.maker).data)
        sell = self._order(maker_sells=True)
        self.assertNotIn("lightning_take_swap", self._get(sell, self.taker).data)

    def test_seller_escrow_page_shows_the_surcharge(self):
        order = self._hybrid_b()
        _, surcharge = self._expected(order)
        with patch("api.logics.LNNode") as ln:
            ln.gen_hold_invoice.side_effect = lambda sats, *a, **k: {
                "invoice": f"lnbcrt{uuid.uuid4().hex}", "preimage": uuid.uuid4().hex,
                "payment_hash": uuid.uuid4().hex, "created_at": timezone.now(),
                "expires_at": timezone.now() + timezone.timedelta(hours=1), "cltv_expiry": 100,
            }
            r = self._get(order, self.taker)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["swap_surcharge_sats"], surcharge)
        order.refresh_from_db()
        self.assertEqual(r.data["escrow_satoshis"], order.trade_escrow.num_satoshis)
        self.assertEqual(
            order.trade_escrow.num_satoshis,
            Logics.escrow_amount(order, self.taker)[1]["escrow_amount"],
        )
