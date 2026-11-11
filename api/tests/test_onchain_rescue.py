"""
Onchain rescue of a failed lightning payout (Roblosats).

When the lightning payout to the buyer fails (order in FAI), the buyer can switch
to an onchain address with `update_address`. The coordinator then cancels the
lightning payout with a conditional update, asks LND whether any attempt is still
in flight or succeeded, and only then queues the onchain payment and completes the
order. These tests cover the happy path and every refusal, plus the guards that
stop a lightning retry once the buyer has switched.
"""

import os
import uuid
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from api.logics import Logics, payout_problem
from api.models import LNPayment, OnchainPayment, Order

ENV = {"DISABLE_ONCHAIN": "False", "MIN_SWAP_AMOUNT": "20000", "MAX_SWAP_AMOUNT": "5000000"}
BALANCE = {
    "total_balance": 10_000_000,
    "confirmed_balance": 10_000_000,
    "unconfirmed_balance": 0,
}
CHANNELS = {
    "local_balance": 5_000_000,
    "remote_balance": 5_000_000,
    "unsettled_local_balance": 0,
    "unsettled_remote_balance": 0,
}
ADDRESS = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


def _user():
    return User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")


def _hold(concept, sender, status, sats=100_000):
    now = timezone.now()
    return LNPayment.objects.create(
        type=LNPayment.Types.HOLD,
        concept=concept,
        status=status,
        payment_hash=uuid.uuid4().hex,
        preimage=uuid.uuid4().hex,
        num_satoshis=sats,
        created_at=now,
        expires_at=now + timezone.timedelta(hours=1),
        sender=sender,
    )


def _fai_order(payout_status=LNPayment.Status.FAILRO):
    buyer, seller = _user(), _user()
    now = timezone.now()
    payout = LNPayment.objects.create(
        type=LNPayment.Types.NORM,
        concept=LNPayment.Concepts.PAYBUYER,
        status=payout_status,
        payment_hash=uuid.uuid4().hex,
        num_satoshis=99_000,
        created_at=now,
        expires_at=now + timezone.timedelta(hours=1),
        receiver=buyer,
        routing_attempts=1,
        last_routing_time=now,
    )
    order = Order.objects.create(
        type=Order.Types.BUY,
        status=Order.Status.FAI,
        created_at=now,
        expires_at=now + timezone.timedelta(hours=24),
        has_range=False,
        amount=100,
        satoshis=100_000,
        t0_satoshis=100_000,
        last_satoshis=100_000,
        maker=buyer,
        taker=seller,
        maker_bond=_hold(LNPayment.Concepts.MAKEBOND, buyer, LNPayment.Status.RETNED),
        taker_bond=_hold(LNPayment.Concepts.TAKEBOND, seller, LNPayment.Status.RETNED),
        trade_escrow=_hold(LNPayment.Concepts.TRESCROW, seller, LNPayment.Status.SETLED, 101_000),
        payout=payout,
        bond_size=3,
    )
    return order, buyer, seller


class OnchainRescueTest(TestCase):
    def _rescue(self, order, user, ln_status=None, ln_raises=None, settled=True):
        node = MagicMock()
        if ln_raises:
            node.payment_status.side_effect = ln_raises
        else:
            node.payment_status.return_value = ln_status
        node.double_check_htlc_is_settled.return_value = settled
        balance_node = MagicMock()
        balance_node.wallet_balance.return_value = BALANCE
        balance_node.channel_balance.return_value = CHANNELS
        with (
            patch.dict(os.environ, ENV),
            patch("api.logics.LNNode", node),
            patch("control.models.LNNode", balance_node),
            patch("api.logics.validate_onchain_address", return_value=(True, None)),
            patch("api.logics.get_minning_fee", return_value=3),
            patch("api.logics.send_notification") as notify,
            patch("api.logics.send_devfund_donation"),
            patch("api.logics.nostr_send_order_event"),
            patch("api.models.order.send_notification"),
        ):
            result = Logics.update_address(order, user, ADDRESS, None)
        return result, node, notify

    def test_happy_path_cancels_lightning_and_queues_onchain(self):
        order, buyer, _ = _fai_order()
        (valid, context), node, notify = self._rescue(order, buyer)
        self.assertTrue(valid, context)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)
        self.assertTrue(order.is_swap)
        self.assertEqual(order.payout.status, LNPayment.Status.CANCEL)
        self.assertEqual(order.payout_tx.status, OnchainPayment.Status.QUEUE)
        self.assertEqual(order.payout_tx.address, ADDRESS)
        self.assertLessEqual(order.payout_tx.num_satoshis, order.trade_escrow.num_satoshis)
        node.payment_status.assert_called_once_with(order.payout.payment_hash)
        notify.delay.assert_called_once()

    def test_expired_invoice_can_be_rescued(self):
        order, buyer, _ = _fai_order(LNPayment.Status.EXPIRE)
        (valid, _), _, _ = self._rescue(order, buyer)
        self.assertTrue(valid)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)

    def test_refused_when_lnd_shows_payment_in_flight(self):
        order, buyer, _ = _fai_order()
        (valid, context), _, notify = self._rescue(order, buyer, ln_status=LNPayment.Status.FLIGHT)
        self.assertFalse(valid)
        self.assertIn("still in progress", context["bad_address"])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FAI)
        self.assertFalse(order.is_swap)
        self.assertEqual(order.payout.status, LNPayment.Status.FAILRO)
        self.assertNotEqual(order.payout_tx.status, OnchainPayment.Status.QUEUE)
        notify.delay.assert_not_called()

    def test_refused_when_lightning_succeeded_late(self):
        order, buyer, _ = _fai_order()
        (valid, _), _, _ = self._rescue(order, buyer, ln_status=LNPayment.Status.SUCCED)
        self.assertFalse(valid)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FAI)
        self.assertEqual(order.payout.status, LNPayment.Status.FAILRO)

    def test_refused_when_lnd_cannot_be_checked(self):
        order, buyer, _ = _fai_order(LNPayment.Status.EXPIRE)
        (valid, context), _, _ = self._rescue(order, buyer, ln_raises=RuntimeError("down"))
        self.assertFalse(valid)
        self.assertIn("Could not check", context["bad_address"])
        order.refresh_from_db()
        self.assertEqual(order.payout.status, LNPayment.Status.EXPIRE)
        self.assertEqual(order.status, Order.Status.FAI)

    def test_refused_when_escrow_not_settled_in_lnd(self):
        order, buyer, _ = _fai_order()
        (valid, _), _, _ = self._rescue(order, buyer, settled=False)
        self.assertFalse(valid)
        order.refresh_from_db()
        self.assertEqual(order.payout.status, LNPayment.Status.FAILRO)
        self.assertEqual(order.status, Order.Status.FAI)

    def test_refused_while_payout_is_in_flight_in_database(self):
        order, buyer, _ = _fai_order(LNPayment.Status.FLIGHT)
        (valid, _), node, _ = self._rescue(order, buyer)
        self.assertFalse(valid)
        node.payment_status.assert_not_called()

    def test_seller_cannot_rescue(self):
        order, _, seller = _fai_order()
        (valid, _), _, _ = self._rescue(order, seller)
        self.assertFalse(valid)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FAI)

    def test_concurrent_request_loses_the_conditional_update(self):
        order, buyer, _ = _fai_order()
        LNPayment.objects.filter(pk=order.payout.pk).update(status=LNPayment.Status.CANCEL)
        (valid, context), node, _ = self._rescue(order, buyer)
        self.assertFalse(valid)
        self.assertIn("still in progress", context["bad_address"])
        node.payment_status.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.FAI)

    def test_second_rescue_is_refused(self):
        order, buyer, _ = _fai_order()
        (first, _), _, _ = self._rescue(order, buyer)
        self.assertTrue(first)
        order.refresh_from_db()
        (second, _), _, _ = self._rescue(order, buyer)
        self.assertFalse(second)


class PayoutProblemAfterRescueTest(TestCase):
    def _order(self, is_swap, status):
        payout = MagicMock(spec=LNPayment)
        payout.concept = LNPayment.Concepts.PAYBUYER
        payout.status = status
        payout.get_status_display.return_value = LNPayment.Status(status).label
        order = MagicMock(spec=Order)
        order.status = Order.Status.FAI
        order.is_swap = is_swap
        return payout, order

    def test_swapped_order_holds_lightning_retry(self):
        payout, order = self._order(True, LNPayment.Status.FAILRO)
        with patch("api.logics.Order.objects") as objects:
            objects.filter.return_value.first.return_value = order
            self.assertIn("onchain", payout_problem(payout))

    def test_cancelled_payout_is_not_retried(self):
        payout, order = self._order(False, LNPayment.Status.CANCEL)
        with patch("api.logics.Order.objects") as objects:
            objects.filter.return_value.first.return_value = order
            self.assertIn("payout status", payout_problem(payout))


class FollowSendPaymentLockTest(TestCase):
    def test_lock_released_when_lnd_starts_the_payment(self):
        from api.tasks import follow_send_payment

        lnp = MagicMock(spec=LNPayment)
        lnp.payment_hash = uuid.uuid4().hex
        lnp.num_satoshis = 90_000
        lnp.routing_budget_ppm = 1_000
        order = MagicMock(spec=Order)
        order.pk = 42
        events = []

        def fake_pay(lnpayment, fee, timeout, on_started=None):
            events.append("pay")
            on_started()
            events.append("after-start")

        with (
            patch("api.models.LNPayment.objects") as lnp_objects,
            patch("api.models.Order.objects") as order_objects,
            patch("api.logics.payout_problem", return_value=None),
            patch("api.logics.acquire_order_lock", side_effect=lambda pk: events.append("lock")),
            patch("api.logics.release_order_lock", side_effect=lambda pk: events.append("unlock")),
            patch("api.lightning.node.LNNode.follow_send_payment", side_effect=fake_pay),
        ):
            lnp_objects.get.return_value = lnp
            order_objects.filter.return_value.first.return_value = order
            follow_send_payment(lnp.payment_hash)

        self.assertEqual(events, ["lock", "pay", "unlock", "after-start"])


class AuditDoublePayoutTest(TestCase):
    def test_lightning_and_onchain_both_paid_is_reported(self):
        order, buyer, _ = _fai_order(LNPayment.Status.SUCCED)
        balance_node = MagicMock()
        balance_node.wallet_balance.return_value = BALANCE
        balance_node.channel_balance.return_value = CHANNELS
        with patch("control.models.LNNode", balance_node):
            tx = OnchainPayment.objects.create(
                receiver=buyer, status=OnchainPayment.Status.QUEUE, num_satoshis=99_000
            )
        order.payout_tx = tx
        order.save(update_fields=["payout_tx"])
        out = StringIO()
        call_command("audit_payouts", "--no-lnd", stdout=out)
        self.assertIn(f"order {order.id}: lightning payout", out.getvalue())
