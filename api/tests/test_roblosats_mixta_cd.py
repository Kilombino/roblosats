"""Roblosats mixta C/D (26 nov 2026): a Lightning order taken with an onchain bond.

Mocked unit tests (no node). Mixta C: the buyer takes a maker's Lightning SELL with an onchain
bond; the order stays a Lightning order and the taker's onchain bond is held through a proxy
LNPayment. Mixta D: the seller takes a maker's Lightning BUY with an onchain bond; the order
becomes an onchain-escrow order and the maker keeps its Lightning hold-invoice bond
(order.maker_lightning). The onchain parts against a node are in
test_escrow_service_regtest.py (EscrowMixtaCDRegtest).
"""

import uuid
from unittest.mock import patch

from decouple import config
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api.escrow import flow
from api.escrow.flow import EscrowError
from api.logics import Logics
from api.models import Currency, LNPayment, OnchainBond, OnchainEscrow, Order, TakeOrder


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


def _hold(sender, concept, sats=30_000, status=LNPayment.Status.LOCKED):
    now = timezone.now()
    return LNPayment.objects.create(
        concept=concept,
        type=LNPayment.Types.HOLD,
        sender=sender,
        status=status,
        num_satoshis=sats,
        payment_hash=uuid.uuid4().hex,
        preimage=uuid.uuid4().hex,
        invoice=f"lnbcrt{uuid.uuid4().hex}",
        created_at=now,
        expires_at=now + timezone.timedelta(hours=3),
    )


class MixtaBase(TestCase):
    def setUp(self):
        self.patches = [
            patch("api.logics.send_notification"),
            patch("api.logics.nostr_send_order_event"),
            patch("api.logics.send_devfund_donation"),
            patch("api.models.order.send_notification"),
            patch("api.escrow.flow.onchain_take_enabled", return_value=True),
        ]
        for p in self.patches:
            p.start()
        User.objects.get_or_create(username="admin")
        self.currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _order(self, maker_sells):
        """A public Lightning order with the maker's bond locked (a hold invoice)."""
        now = timezone.now()
        self.maker, self.taker = _user(), _user()
        order = Order.objects.create(
            currency=self.currency,
            type=Order.Types.SELL if maker_sells else Order.Types.BUY,
            status=Order.Status.PUB,
            escrow_type=Order.EscrowTypes.LIGHTNING,
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
        order.maker_bond = _hold(self.maker, LNPayment.Concepts.MAKEBOND)
        order.save(update_fields=["maker_bond"])
        return order

    def _onchain_take(self, order, taker=None, lock=True):
        taker = taker or self.taker
        Logics.take(order, taker, onchain=True)
        take = TakeOrder.objects.filter(order=order, taker=taker).latest("id")
        bond = OnchainBond.objects.create(
            order=order,
            take_order=take,
            owner=taker,
            role="taker",
            owner_pubkey="02" + "11" * 32,
            coordinator_pubkey="02" + "22" * 32,
            refund_csv=144,
            address=f"bcrt1ptakerbond{uuid.uuid4().hex[:6]}",
            script_pubkey="5120" + "33" * 32,
            required_sats=30_000,
            txid="aa" * 32 if lock else "",
            vout=0,
            value=30_000 if lock else 0,
            status=OnchainBond.Status.LOCKED if lock else OnchainBond.Status.FUNDING,
        )
        return take, bond

    def _contract(self, order, take):
        with patch("api.logics.escrow_flow.on_contract"), patch("api.logics.MarketTick.log_a_tick"):
            Logics.finalize_contract(take)
        order.refresh_from_db()
        return order


class TakeOnchain(MixtaBase):
    def test_onchain_take_of_a_lightning_order_gets_the_onchain_window(self):
        order = self._order(maker_sells=True)
        Logics.take(order, self.taker, onchain=True)
        take = TakeOrder.objects.get(order=order)
        self.assertTrue(take.onchain)
        self.assertFalse(take.lightning)
        left = (take.expires_at - timezone.now()).total_seconds()
        window = config("ESCROW_BOND_SECONDS", cast=int, default=3 * 3600)
        self.assertGreater(left, window - 60)  # the onchain window (3 h), not the 150 s hold invoice

    def test_onchain_flag_ignored_on_an_onchain_order(self):
        order = self._order(maker_sells=True)
        order.escrow_type = Order.EscrowTypes.ONCHAIN
        order.save()
        Logics.take(order, self.taker, onchain=True)
        self.assertFalse(TakeOrder.objects.get(order=order).onchain)

    def test_lightning_take_gets_no_onchain_bond(self):
        order = self._order(maker_sells=True)
        Logics.take(order, self.taker)
        with self.assertRaises(EscrowError):
            flow.bond_action(order, self.taker, "bond_key", {"bond_pubkey": "02" + "11" * 32})

    def test_maker_of_a_lightning_order_gets_no_onchain_bond(self):
        order = self._order(maker_sells=True)
        with self.assertRaises(EscrowError):
            flow.bond_action(order, self.maker, "bond_key", {"bond_pubkey": "02" + "11" * 32})

    def test_mempool_pushes_the_take_deadline_once(self):
        from django.core.cache import cache

        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order, lock=False)
        cache.delete(f"onchain-take-mempool:{take.pk}")
        take.expires_at = timezone.now() + timezone.timedelta(seconds=30)
        take.save(update_fields=["expires_at"])
        window = config("ESCROW_BOND_SECONDS", cast=int, default=3 * 3600)
        with patch("api.escrow.flow._unconfirmed", return_value=[("bb" * 32, 30_000)]):
            self.assertTrue(flow.extend_take_from_mempool(take, bond))
            self.assertFalse(flow.extend_take_from_mempool(take, bond))  # only the first time
        take.refresh_from_db()
        self.assertGreater((take.expires_at - timezone.now()).total_seconds(), window - 60)
        cache.delete(f"onchain-take-mempool:{take.pk}")


class MixtaC(MixtaBase):
    """The buyer takes a maker's Lightning SELL with an onchain bond."""

    def test_stays_lightning_with_a_proxy_taker_bond(self):
        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        self.assertEqual(order.escrow_type, Order.EscrowTypes.LIGHTNING)
        self.assertEqual(order.status, Order.Status.WF2)
        self.assertEqual(order.taker, self.taker)
        self.assertTrue(flow.has_taker_proxy(order))
        self.assertFalse(flow.has_proxy(order))
        self.assertEqual(flow.proxied_bond(order.taker_bond), bond)
        self.assertEqual(order.taker_bond.concept, LNPayment.Concepts.TAKEBOND)
        self.assertEqual(order.taker_bond.status, LNPayment.Status.LOCKED)
        self.assertEqual(flow.hybrid_kind(order), "C")
        self.assertFalse(order.maker_lightning)

    def test_settling_the_taker_proxy_penalizes_the_onchain_bond(self):
        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        with patch("api.escrow.bonds.penalize", return_value=True) as pen:
            self.assertTrue(Logics.settle_bond(order.taker_bond))
            self.assertFalse(Logics.settle_bond(order.taker_bond))
        pen.assert_called_once()

    def test_returning_the_taker_proxy_frees_the_onchain_bond(self):
        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        with patch("api.logics.LNNode") as ln:
            Logics.return_bond(order.taker_bond)
            ln.cancel_return_hold_invoice.assert_not_called()
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)

    def test_taker_back_out_publishes_a_plain_lightning_order(self):
        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        Logics.publish_order(order)
        order.refresh_from_db()
        self.assertEqual(order.escrow_type, Order.EscrowTypes.LIGHTNING)
        self.assertIsNone(order.taker_bond)
        self.assertEqual(order.maker_bond.status, LNPayment.Status.LOCKED)

    def test_reward_gate_accepts_a_penalized_taker_proxy(self):
        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        proxy = order.taker_bond
        LNPayment.objects.filter(pk=proxy.pk).update(status=LNPayment.Status.SETLED)
        OnchainBond.objects.filter(pk=bond.pk).update(status=OnchainBond.Status.PENALIZED)
        with patch("api.logics.LNNode") as ln:
            problem = Logics.reward_source_problem(order, proxy, order.maker_bond, 15_000)
            ln.double_check_htlc_is_settled.assert_not_called()
        self.assertIsNone(problem)

    def test_dispute_keeps_the_onchain_taker_bond_locked_until_decided(self):
        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        with patch("api.escrow.bonds.penalize_now") as pen, patch("api.escrow.bonds.release") as rel:
            self.assertEqual(flow.dispute_bond_sats(order, "taker", wins=False), 0)
            pen.assert_called_once()
            rel.assert_not_called()
        bond.refresh_from_db()
        with patch("api.escrow.bonds.release") as rel:
            OnchainBond.objects.filter(pk=bond.pk).update(status=OnchainBond.Status.LOCKED)
            self.assertEqual(flow.dispute_bond_sats(order, "taker", wins=True), 0)
            rel.assert_called_once()

    def test_onchain_payout_minimum_relaxed_for_the_onchain_bond_buyer(self):
        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        self.assertTrue(flow.has_any_proxy(order))


class MixtaD(MixtaBase):
    """The seller takes a maker's Lightning BUY with an onchain bond."""

    def test_becomes_onchain_with_the_maker_lightning_bond(self):
        order = self._order(maker_sells=False)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        self.assertEqual(order.escrow_type, Order.EscrowTypes.ONCHAIN)
        self.assertTrue(order.maker_lightning)
        self.assertFalse(order.taker_lightning)
        self.assertIsNone(order.taker_bond)
        self.assertEqual(flow.ln_side(order), "maker")
        self.assertTrue(flow.role_is_ln(order, "buyer"))
        self.assertFalse(flow.role_is_ln(order, "seller"))
        self.assertTrue(flow.buyer_fee_at_release(order))
        self.assertEqual(flow.bonds_locked(order), (True, True))
        self.assertEqual(flow.bond_of(order, "taker"), bond)
        self.assertEqual(flow.hybrid_kind(order), "D")

    def test_escrow_plan_is_the_lightning_buyer_one(self):
        from api.escrow import service

        order = self._order(maker_sells=False)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        self.assertTrue(service._buyer_has_ln_bond(order))

    def test_seller_at_fault_penalizes_onchain_and_frees_the_lightning_bond(self):
        order = self._order(maker_sells=False)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        with patch("api.logics.Logics.return_bond", return_value=True) as ret, \
                patch("api.escrow.bonds.penalize") as pen:
            flow.penalize_side(order, "taker")
            flow.free_side(order, "maker")
        pen.assert_called_once()
        ret.assert_called_once_with(order.maker_bond)

    def test_buyer_at_fault_settles_its_lightning_bond(self):
        order = self._order(maker_sells=False)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        with patch("api.logics.Logics.settle_bond", return_value=True) as settle, \
                patch("api.escrow.bonds.penalize") as pen, patch("api.escrow.bonds.release") as rel:
            flow.penalize_side(order, "maker")
            flow.free_side(order, "taker")
        settle.assert_called_once_with(order.maker_bond)
        pen.assert_not_called()
        rel.assert_called_once()

    def test_dispute_credits_the_maker_lightning_bond(self):
        order = self._order(maker_sells=False)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        with patch("api.logics.ledger_credit", return_value=True) as credit:
            flow.ln_bond_dispute_credit(order, buyer_wins=True)
        robot, sats, kind = credit.call_args.args[:3]
        self.assertEqual((robot, sats, kind), (self.maker.robot, 30_000, "dispute_bond_back"))
        with patch("api.logics.ledger_credit", return_value=True) as credit:
            flow.ln_bond_dispute_credit(order, buyer_wins=False)
        robot, sats, kind = credit.call_args.args[:3]
        self.assertEqual((robot, sats, kind), (self.taker.robot, 15_000, "slash_reward"))

    def test_slashed_lightning_maker_bond_rewards_the_onchain_seller(self):
        order = self._order(maker_sells=False)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        LNPayment.objects.filter(pk=order.maker_bond.pk).update(status=LNPayment.Status.SETLED)
        order.refresh_from_db()
        side = flow._slash_side(order, "maker")
        self.assertEqual((side["staked"], side["received"]), (30_000, 30_000))
        self.assertEqual(side["user"], self.maker)

    def test_seller_cancel_sends_it_back_as_a_lightning_order(self):
        order = self._order(maker_sells=False)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        OnchainEscrow.objects.create(order=order, coordinator_pubkey="02" + "44" * 32, rescue_csv=144)
        with patch("api.escrow.bonds.penalize", return_value=True), \
                patch("api.escrow.flow.credit_penalty", return_value=True):
            flow.cancel_before_escrow(order, "taker")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PUB)
        self.assertEqual(order.escrow_type, Order.EscrowTypes.LIGHTNING)
        self.assertFalse(order.maker_lightning)
        self.assertIsNone(order.taker)
        self.assertEqual(order.maker_bond.status, LNPayment.Status.LOCKED)
        self.assertFalse(OnchainEscrow.objects.filter(order=order).exists())


class Race(MixtaBase):
    def test_lightning_take_wins_and_frees_the_onchain_take(self):
        order = self._order(maker_sells=True)
        onchain_take, bond = self._onchain_take(order, lock=False)
        other = _user()
        Logics.take(order, other)
        ln_take = TakeOrder.objects.get(order=order, taker=other)
        ln_take.taker_bond = _hold(other, LNPayment.Concepts.TAKEBOND)
        ln_take.save(update_fields=["taker_bond"])
        with patch("api.logics.escrow_flow.on_contract"), patch("api.logics.MarketTick.log_a_tick"), \
                patch("api.escrow.flow.send_notification", create=True), \
                patch("api.tasks.send_notification") as notify, \
                patch("api.escrow.bonds.refresh", side_effect=lambda b, *a, **k: b):
            Logics.finalize_contract(ln_take)
        order.refresh_from_db()
        self.assertEqual(order.taker, other)
        self.assertFalse(flow.has_taker_proxy(order))
        bond.refresh_from_db()
        self.assertIn(bond.status, (OnchainBond.Status.ABANDONED, OnchainBond.Status.RETURNABLE))
        onchain_take.refresh_from_db()
        self.assertLessEqual(onchain_take.expires_at, timezone.now())
        notify.delay.assert_called()

    def test_oldest_locked_onchain_take_wins(self):
        order = self._order(maker_sells=True)
        first, b1 = self._onchain_take(order)
        second_user = _user()
        second, b2 = self._onchain_take(order, taker=second_user)
        with patch("api.escrow.bonds.refresh", side_effect=lambda b, *a, **k: b), \
                patch("api.escrow.flow.extend_take_from_mempool"), \
                patch("api.logics.escrow_flow.on_contract"), patch("api.logics.MarketTick.log_a_tick"), \
                patch("api.tasks.send_notification"):
            flow.advance_onchain_takes(order)
        order.refresh_from_db()
        self.assertEqual(order.taker, self.taker)
        self.assertEqual(flow.proxied_bond(order.taker_bond), b1)
        b2.refresh_from_db()
        self.assertEqual(b2.status, OnchainBond.Status.RETURNABLE)


class TakeOnchainView(MixtaBase):
    """The take request with take_onchain on a Lightning order, through the view."""

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

    @patch("api.escrow.service.rpc", side_effect=lambda m, *a, **k: {"descriptor": "addr(x)#y"} if m == "getdescriptorinfo" else [])
    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_onchain_take_gets_no_invoice_and_opens_a_bond(self, _allowed, _rpc):
        order = self._order(maker_sells=True)
        with patch("api.logics.LNNode.gen_hold_invoice") as gen:
            r = self._call("post", order, self.taker, {"action": "take", "take_onchain": True})
            gen.assert_not_called()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIs(r.data["take_onchain"], True)
        self.assertIs(r.data["take_lightning"], False)
        self.assertEqual(r.data["status"], Order.Status.TAK)
        self.assertTrue(TakeOrder.objects.get(order=order).onchain)
        r = self._call("post", order, self.taker, {"action": "bond_key", "escrow": {"bond_pubkey": "02" + "44" * 32}})
        self.assertEqual(r.status_code, 200, r.data)
        bond = OnchainBond.objects.get(order=order, owner=self.taker, role="taker")
        self.assertEqual(bond.required_sats, 30_000)
        self.assertEqual(r.data["bond_role"], "taker")

    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_onchain_take_refused_when_off(self, _allowed):
        order = self._order(maker_sells=True)
        with patch("api.escrow.flow.onchain_take_enabled", return_value=False):
            r = self._call("post", order, self.taker, {"action": "take", "take_onchain": True})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(TakeOrder.objects.filter(order=order).exists())

    @patch("api.views.escrow_flow.robot_allowed", return_value=True)
    def test_mixta_c_taker_sees_its_onchain_bond(self, _allowed):
        order = self._order(maker_sells=True)
        take, bond = self._onchain_take(order)
        order = self._contract(order, take)
        with patch("api.logics.Logics.payout_amount", return_value=(True, {"invoice_amount": 99_000, "swap_allowed": False})):
            r = self._call("get", order, self.taker)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["hybrid_onchain_taker_bond"])
        self.assertEqual(r.data["hybrid_kind"], "C")
        self.assertEqual(r.data["bond_role"], "taker")
        self.assertEqual(r.data["bond_status_code"], OnchainBond.Status.LOCKED)
        self.assertTrue(r.data["taker_locked"])


class MixtaDWindow(MixtaBase):
    """TG 15602: the maker's Lightning bond outlives the onchain flow of a mixta D order."""

    def _with_cltv(self, order, blocks, age_h):
        b = order.maker_bond
        b.cltv_expiry = blocks
        b.created_at = timezone.now() - timezone.timedelta(hours=age_h)
        b.save()
        return order

    def test_a_fresh_maker_bond_has_room(self):
        from api.logics import Logics as L

        order = self._order(maker_sells=False)
        blocks = L.compute_cltv_expiry_blocks(order, "maker_bond")
        ok, left, needed = flow.mixta_d_window(self._with_cltv(order, blocks, 0))
        self.assertTrue(ok, (left, needed))
        # and still with the whole public time used up and a whole onchain take window on top
        aged = (order.t_to_expire(Order.Status.PUB) + config("ESCROW_BOND_SECONDS", cast=int, default=3 * 3600)) / 3600
        ok, left, needed = flow.mixta_d_window(self._with_cltv(order, blocks, aged))
        self.assertTrue(ok, (left, needed))

    def test_an_old_maker_bond_refuses_the_onchain_seller(self):
        order = self._with_cltv(self._order(maker_sells=False), 100, 24)
        ok, left, needed = flow.mixta_d_window(order)
        self.assertFalse(ok)


class PendingTakesForMaker(MixtaBase):
    """Kilombino TG 15618: the maker of a public order sees the onchain takes on their way
    (counts only) and is told once per take when its bond reaches the mempool."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        cache.clear()

    def test_counts_by_stage_without_identity(self):
        order = self._order(maker_sells=True)
        take1, bond1 = self._onchain_take(order, lock=False)
        take2, _ = self._onchain_take(order, taker=_user(), lock=False)
        self.assertEqual(flow.pending_onchain_takes(order), {"mempool": 0, "confirmed": 0, "awaiting_payment": 2})
        with patch("api.escrow.flow._unconfirmed", return_value=[{"txid": "bb" * 32}]), \
                patch("api.step_alerts.queue") as q:
            self.assertTrue(flow.extend_take_from_mempool(take1, bond1))
            self.assertFalse(flow.extend_take_from_mempool(take1, bond1))  # once per take
        q.assert_called_once()
        self.assertEqual(q.call_args.args[1], order.maker)
        self.assertEqual(q.call_args.args[2], "take_in_mempool")
        self.assertEqual(flow.pending_onchain_takes(order), {"mempool": 1, "confirmed": 0, "awaiting_payment": 1})
        OnchainBond.objects.filter(take_order=take2).update(status=OnchainBond.Status.SIGN)
        self.assertEqual(flow.pending_onchain_takes(order), {"mempool": 1, "confirmed": 1, "awaiting_payment": 0})
        TakeOrder.objects.filter(pk=take1.pk).update(expires_at=timezone.now() - timezone.timedelta(seconds=1))
        self.assertEqual(flow.pending_onchain_takes(order)["mempool"], 0)

    def test_order_get_shows_it_to_the_maker_only(self):
        from rest_framework.test import APIClient

        order = self._order(maker_sells=True)
        self._onchain_take(order, lock=False)
        c = APIClient()
        with patch("api.views.escrow_flow.order_data", return_value={}), \
                patch("api.logics.Logics.price_and_premium_now", return_value=(1, 0)):
            c.force_authenticate(self.maker)
            r = c.get(f"/api/order/?order_id={order.id}")
            self.assertEqual(r.status_code, 200, r.data)
            self.assertEqual(r.data["pending_onchain_takes"]["awaiting_payment"], 1)
            c.force_authenticate(self.taker)
            r = c.get(f"/api/order/?order_id={order.id}")
            self.assertNotIn("pending_onchain_takes", r.data)

    def test_alert_text_in_three_languages(self):
        from api import step_alerts

        for lang in ("en", "es", "ca"):
            t = step_alerts.text("take_in_mempool", lang, n=35)
            self.assertIn("35", t)
            self.assertIn("⛓️", t)
