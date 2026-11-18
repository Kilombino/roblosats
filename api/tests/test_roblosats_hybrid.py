"""Roblosats hybrid (rl31-beta10): a Lightning taker on an onchain order.

Mocked unit tests (no node): the take flag, the case B switch to a Lightning order with a
proxy of the onchain maker bond, what settling/returning that proxy does, the way back to an
onchain order, the reward gate for a proxy, the hybrid swap minimum and the case A rules
(Lightning taker bond: no buyer funding, fee out of the payout, LN bond penalized/freed).
The onchain parts against a node are in test_escrow_service_regtest.py (EscrowHybridRegtest).
"""

import uuid
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api.escrow import flow
from api.logics import Logics
from api.models import Currency, LNPayment, OnchainBond, Order, TakeOrder


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


class HybridBase(TestCase):
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
        self.currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _order(self, maker_sells, status=Order.Status.PUB):
        now = timezone.now()
        self.maker, self.taker = _user(), _user()
        order = Order.objects.create(
            currency=self.currency,
            type=Order.Types.SELL if maker_sells else Order.Types.BUY,
            status=status,
            escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now,
            expires_at=now + timezone.timedelta(hours=24),
            has_range=False,
            amount=100,
            satoshis=1_000_000,
            t0_satoshis=1_000_000,
            last_satoshis=1_000_000,
            is_explicit=True,
            maker=self.maker,
            bond_size=3,
        )
        self.bond = OnchainBond.objects.create(
            order=order,
            owner=self.maker,
            role="maker",
            owner_pubkey="02" + "11" * 32,
            coordinator_pubkey="02" + "22" * 32,
            refund_csv=144,
            address="bcrt1pmakerbond",
            script_pubkey="5120" + "33" * 32,
            required_sats=30_000,
            txid="aa" * 32,
            vout=0,
            value=30_000,
            status=OnchainBond.Status.LOCKED,
        )
        return order

    def _ln_take(self, order):
        Logics.take(order, self.taker, lightning=True)
        take = TakeOrder.objects.get(order=order, taker=self.taker)
        now = timezone.now()
        take.taker_bond = LNPayment.objects.create(
            concept=LNPayment.Concepts.TAKEBOND,
            type=LNPayment.Types.HOLD,
            sender=self.taker,
            status=LNPayment.Status.LOCKED,
            num_satoshis=30_000,
            payment_hash=uuid.uuid4().hex,
            preimage=uuid.uuid4().hex,
            invoice=f"lnbcrt{uuid.uuid4().hex}",
            created_at=now,
            expires_at=now + timezone.timedelta(hours=3),
        )
        take.save(update_fields=["taker_bond"])
        with patch("api.logics.escrow_flow.on_contract"), patch("api.logics.MarketTick.log_a_tick"):
            Logics.finalize_contract(take)
        order.refresh_from_db()
        return order


class TakeFlag(HybridBase):
    def test_take_with_lightning_marks_the_take(self):
        order = self._order(maker_sells=True)
        Logics.take(order, self.taker, lightning=True)
        self.assertTrue(TakeOrder.objects.get(order=order).lightning)

    def test_lightning_flag_ignored_on_a_lightning_order(self):
        order = self._order(maker_sells=True)
        order.escrow_type = Order.EscrowTypes.LIGHTNING
        order.save()
        Logics.take(order, self.taker, lightning=True)
        self.assertFalse(TakeOrder.objects.get(order=order).lightning)


class CaseB(HybridBase):
    """The seller (taker) takes a maker's onchain BUY with Lightning."""

    def test_switches_to_lightning_with_a_proxy_bond(self):
        order = self._ln_take(self._order(maker_sells=False))
        self.assertEqual(order.escrow_type, Order.EscrowTypes.LIGHTNING)
        self.assertTrue(order.taker_lightning)
        self.assertTrue(flow.has_proxy(order))
        self.assertEqual(order.maker_bond.status, LNPayment.Status.LOCKED)
        self.assertEqual(order.maker_bond.num_satoshis, 30_000)
        self.assertEqual(flow.proxied_bond(order.maker_bond), self.bond)
        self.assertEqual(order.status, Order.Status.WF2)

    def test_settling_the_proxy_penalizes_the_onchain_bond_once(self):
        order = self._ln_take(self._order(maker_sells=False))
        with patch("api.escrow.bonds.penalize", return_value=True) as pen:
            self.assertTrue(Logics.settle_bond(order.maker_bond))
            self.assertFalse(Logics.settle_bond(order.maker_bond))
        pen.assert_called_once()
        order.maker_bond.refresh_from_db()
        self.assertEqual(order.maker_bond.status, LNPayment.Status.SETLED)

    def test_returning_the_proxy_frees_the_onchain_bond(self):
        order = self._ln_take(self._order(maker_sells=False))
        with patch("api.logics.LNNode") as ln:
            Logics.return_bond(order.maker_bond)
            ln.cancel_return_hold_invoice.assert_not_called()
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)
        order.maker_bond.refresh_from_db()
        self.assertEqual(order.maker_bond.status, LNPayment.Status.RETNED)

    def test_back_in_the_book_is_onchain_again(self):
        order = self._ln_take(self._order(maker_sells=False))
        Logics.publish_order(order)
        order.refresh_from_db()
        self.assertEqual(order.escrow_type, Order.EscrowTypes.ONCHAIN)
        self.assertIsNone(order.maker_bond)
        self.assertFalse(order.taker_lightning)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.LOCKED)

    def test_the_proxy_is_never_looked_up_in_lnd(self):
        from api.management.commands.follow_invoices import Command

        order = self._ln_take(self._order(maker_sells=False))
        LNPayment.objects.filter(pk=order.maker_bond.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=5)
        )
        with patch("api.management.commands.follow_invoices.LNNode") as ln:
            ln.lookup_invoice_status.return_value = (LNPayment.Status.LOCKED, 0)
            Command().follow_hold_invoices()
        looked = [c.args[0].payment_hash for c in ln.lookup_invoice_status.call_args_list]
        self.assertNotIn(order.maker_bond.payment_hash, looked)

    def test_reward_gate_accepts_a_penalized_proxy_without_lnd(self):
        order = self._ln_take(self._order(maker_sells=False))
        proxy = order.maker_bond
        LNPayment.objects.filter(pk=proxy.pk).update(status=LNPayment.Status.SETLED)
        OnchainBond.objects.filter(pk=self.bond.pk).update(status=OnchainBond.Status.PENALIZED)
        with patch("api.logics.LNNode") as ln:
            problem = Logics.reward_source_problem(order, proxy, order.taker_bond, 15_000)
            ln.double_check_htlc_is_settled.assert_not_called()
        self.assertIsNone(problem)

    def test_reward_gate_refuses_a_proxy_not_penalized(self):
        order = self._ln_take(self._order(maker_sells=False))
        proxy = order.maker_bond
        LNPayment.objects.filter(pk=proxy.pk).update(status=LNPayment.Status.SETLED)
        self.assertIn("not penalized", Logics.reward_source_problem(order, proxy, order.taker_bond, 15_000))

    @patch("api.logics.Logics.is_buyer", return_value=True)
    @patch("api.logics.config")
    def test_hybrid_swap_minimum_is_the_onchain_minimum(self, mock_config, _b):
        values = {"MIN_SWAP_AMOUNT": 101_000, "MAX_SWAP_AMOUNT": 6_000_000, "ESCROW_MIN_SATS": 20_000,
                  "DISABLE_ONCHAIN": True}
        mock_config.side_effect = lambda key, **kw: values.get(key, kw.get("default"))
        user = Mock(id=1)
        order = Mock(maker=user, taker=Mock(id=2), last_satoshis=80_000, payout_tx=None, payout=None, log=Mock())
        with patch("api.logics.escrow_flow.has_proxy", return_value=True):
            _, ctx = Logics.payout_amount(order, user)
        # past the minimum: it is the "swaps disabled" switch that stops it here
        self.assertEqual(ctx["swap_failure_reason"], "On-the-fly submarine swaps are disabled")
        with patch("api.logics.escrow_flow.has_proxy", return_value=False):
            _, ctx = Logics.payout_amount(order, user)
        self.assertIn("smaller than the minimum swap", ctx["swap_failure_reason"])


class CaseA(HybridBase):
    """The buyer (taker) takes a maker's onchain SELL with Lightning."""

    def test_stays_onchain_with_a_lightning_taker_bond(self):
        order = self._ln_take(self._order(maker_sells=True))
        self.assertEqual(order.escrow_type, Order.EscrowTypes.ONCHAIN)
        self.assertTrue(order.taker_lightning)
        self.assertIsNone(order.maker_bond)
        self.assertTrue(flow.role_is_ln(order, "buyer"))
        self.assertFalse(flow.role_is_ln(order, "seller"))
        self.assertTrue(flow.buyer_fee_at_release(order))
        self.assertEqual(flow.bonds_locked(order), (True, True))

    def test_taker_at_fault_settles_its_lightning_bond(self):
        order = self._ln_take(self._order(maker_sells=True))
        with patch("api.logics.Logics.settle_bond", return_value=True) as settle, \
                patch("api.escrow.bonds.penalize") as pen, patch("api.escrow.bonds.release") as rel:
            flow.penalize_side(order, "taker")
            flow.free_side(order, "maker")
        settle.assert_called_once_with(order.taker_bond)
        pen.assert_not_called()
        rel.assert_called_once()

    def test_maker_at_fault_returns_the_lightning_bond(self):
        order = self._ln_take(self._order(maker_sells=True))
        with patch("api.logics.Logics.return_bond", return_value=True) as ret, \
                patch("api.escrow.bonds.penalize") as pen:
            flow.penalize_side(order, "maker")
            flow.free_side(order, "taker")
        pen.assert_called_once()
        ret.assert_called_once_with(order.taker_bond)

    def test_dispute_credits_the_lightning_bond(self):
        order = self._ln_take(self._order(maker_sells=True))
        with patch("api.logics.ledger_credit", return_value=True) as credit:
            flow.ln_bond_dispute_credit(order, buyer_wins=True)
        robot, sats, kind = credit.call_args.args[:3]
        self.assertEqual((robot, sats, kind), (self.taker.robot, 30_000, "dispute_bond_back"))
        with patch("api.logics.ledger_credit", return_value=True) as credit:
            flow.ln_bond_dispute_credit(order, buyer_wins=False)
        robot, sats, kind = credit.call_args.args[:3]
        self.assertEqual((robot, sats, kind), (self.maker.robot, 15_000, "slash_reward"))

    def test_pure_onchain_order_unchanged(self):
        order = self._order(maker_sells=True)
        order.taker = self.taker
        order.save()
        self.assertFalse(flow.buyer_fee_at_release(order))
        self.assertFalse(flow.role_is_ln(order, "buyer"))
        self.assertEqual(Decimal(order.amount), Decimal(100))


class TakeView(HybridBase):
    """The take request with take_lightning, through the view."""

    def _call(self, method, order, user, body=None):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from api.views import OrderView

        f = APIRequestFactory()
        if method == "get":
            req = f.get(f"/api/order/?order_id={order.id}")
            view = OrderView.as_view({"get": "get"})
        else:
            req = f.post(f"/api/order/?order_id={order.id}", body, format="json")
            view = OrderView.as_view({"post": "take_update_confirm_dispute_cancel"})
        force_authenticate(req, user=user)
        return view(req)

    @patch("api.logics.LNNode.gen_hold_invoice")
    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_take_with_lightning_gets_a_hold_invoice(self, _allowed, gen):
        now = timezone.now()
        gen.return_value = {
            "invoice": "lnbcrt300u1hybrid", "preimage": "ab" * 32, "payment_hash": "cd" * 32,
            "created_at": now, "expires_at": now + timezone.timedelta(hours=3), "cltv_expiry": 144,
        }
        order = self._order(maker_sells=True)
        r = self._call("post", order, self.taker, {"action": "take", "take_lightning": True})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["bond_invoice"], "lnbcrt300u1hybrid")
        self.assertTrue(r.data["take_lightning"])
        self.assertTrue(TakeOrder.objects.get(order=order).lightning)

    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_onchain_take_gets_no_invoice(self, _allowed):
        order = self._order(maker_sells=True)
        with patch("api.logics.LNNode.gen_hold_invoice") as gen:
            r = self._call("post", order, self.taker, {"action": "take"})
            gen.assert_not_called()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn("bond_invoice", {k for k, v in r.data.items() if v})
        self.assertFalse(TakeOrder.objects.get(order=order).lightning)

    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_case_b_maker_sees_its_onchain_bond(self, _allowed):
        order = self._ln_take(self._order(maker_sells=False))
        with patch("api.logics.Logics.payout_amount", return_value=(True, {"invoice_amount": 99_000, "swap_allowed": False})):
            r = self._call("get", order, self.maker)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["hybrid_onchain_maker_bond"])
        self.assertTrue(r.data["taker_lightning"])
        self.assertEqual(r.data["bond_role"], "maker")
        self.assertEqual(r.data["bond_status_code"], OnchainBond.Status.LOCKED)


class TakeAgain093(TakeView):
    """0.9.3 (Kilobot #24): a Lightning take cancelled, then an onchain take of a 丰 sat order."""

    def _sat_order(self):
        order = self._order(maker_sells=True)
        Order.objects.filter(id=order.id).update(sats_denominated=True, amount=1_000_000, fixed_price=Decimal("0.0021"))
        order.refresh_from_db()
        return order

    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_onchain_take_says_it_is_not_lightning(self, _allowed):
        order = self._sat_order()
        r = self._call("post", order, self.taker, {"action": "take", "amount": 0})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIs(r.data["take_lightning"], False)
        self.assertEqual(r.data["bond_role"], "taker")
        self.assertEqual(Decimal(r.data["amount"]), Decimal(1_000_000))
        self.assertEqual(r.data["exchange_amount"], "0.000021")  # 0.01 BTC at 0.0021 per BTC
        self.assertEqual(r.data["status"], Order.Status.TAK)

    @patch("api.escrow.service.rpc", side_effect=lambda m, *a, **k: {"descriptor": "addr(x)#y"} if m == "getdescriptorinfo" else [])
    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_onchain_take_after_a_cancelled_lightning_take_opens_a_bond(self, _allowed, _rpc):
        order = self._sat_order()
        Logics.take(order, self.taker, lightning=True)
        TakeOrder.objects.filter(order=order, taker=self.taker).update(expires_at=timezone.now())
        other = _user()
        r = self._call("post", order, other, {"action": "take", "amount": 0})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIs(r.data["take_lightning"], False)
        r = self._call("post", order, other, {"action": "bond_key", "escrow": {"bond_pubkey": "02" + "44" * 32}})
        self.assertEqual(r.status_code, 200, r.data)
        bond = OnchainBond.objects.get(order=order, owner=other, role="taker")
        self.assertEqual(bond.required_sats, 30_000)

    @patch("api.logics.LNNode.gen_hold_invoice")
    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_no_onchain_bond_for_a_lightning_take(self, _allowed, gen):
        now = timezone.now()
        gen.return_value = {
            "invoice": "lnbcrt300u1hybrid", "preimage": "ab" * 32, "payment_hash": "cd" * 32,
            "created_at": now, "expires_at": now + timezone.timedelta(hours=3), "cltv_expiry": 144,
        }
        order = self._sat_order()
        r = self._call("post", order, self.taker, {"action": "take", "take_lightning": True})
        self.assertEqual(r.data["bond_invoice"], "lnbcrt300u1hybrid")
        self.assertEqual(r.data["bond_satoshis"], 30_000)
        r = self._call("post", order, self.taker, {"action": "bond_key", "escrow": {"bond_pubkey": "02" + "44" * 32}})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(OnchainBond.objects.filter(order=order, role="taker").exists())
