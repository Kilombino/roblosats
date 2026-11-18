"""
Reward withdrawals whose payment fails before it starts (rl31-beta23).

Live case on Kilobot: a robot submitted an invoice made by the coordinator's own
LND node. SendPaymentV2 raised grpc UNKNOWN "self-payments not allowed", the
exception escaped pay_invoice → HTTP 500, the LNPayment stayed VALIDI and the
robot was left debited (0 rewards, "You have not earned rewards").

Covered here, with a real DB (robot, ledger, LNPayment) and a mocked LND:
  - an invoice of the coordinator's own node is refused before any debit;
  - a pre-flight gRPC error in pay_invoice → FAILRO + refund (available at once)
    + a clear error, never an exception;
  - an error once the payment may exist (stream started, transport error,
    ALREADY_EXISTS) is never refunded unless LND reports it FAILED;
  - follow_send_payment (buyer payouts) fails a pre-flight rejection like a
    FAILED payment instead of retrying it forever.
"""

import os
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import grpc
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from api.lightning import lightning_pb2
from api.lightning.lnd import LNDNode
from api.logics import (
    SELF_INVOICE_ERROR,
    Logics,
    ensure_ledger,
    ledger_add,
    ledger_backed,
)
from api.models import LNPayment, Order, Robot

OWN_PUBKEY = "02" + "aa" * 32
OTHER_PUBKEY = "03" + "bb" * 32


class FakeRpcError(grpc.RpcError):
    def __init__(self, code, details):
        self._code, self._details = code, details

    def code(self):
        return self._code

    def details(self):
        return self._details

    def __str__(self):
        return f"<FakeRpcError {self._code} {self._details}>"


def stream(responses=(), error=None):
    """A SendPaymentV2 stream: yields responses, then raises error (if any)."""

    def gen(*a, **kw):
        for r in responses:
            yield r
        if error is not None:
            raise error

    return gen


def router_with(send):
    stub = MagicMock()
    stub.SendPaymentV2.side_effect = send
    return patch("api.lightning.lnd.router_pb2_grpc.RouterStub", return_value=stub)


IN_FLIGHT = lightning_pb2.Payment(status=lightning_pb2.Payment.PaymentStatus.IN_FLIGHT)
SELF_PAY = FakeRpcError(grpc.StatusCode.UNKNOWN, "self-payments not allowed")


@override_settings(DEBUG=True)
class WithdrawPreflightTest(TestCase):
    def setUp(self):
        User.objects.get_or_create(username="admin")  # ESCROW_USERNAME
        ensure_ledger()  # before the balance exists (no "opening" row for it)
        self.user = User.objects.create_user(username=f"robot{uuid.uuid4().hex[:8]}")
        robot = Robot.objects.get(user=self.user)
        robot.earned_rewards = 750
        robot.save(update_fields=["earned_rewards"])
        ledger_add(robot.pk, 750, "reward", f"test:{uuid.uuid4().hex}")
        self.user.robot = robot

    def robot(self):
        return Robot.objects.get(pk=self.user.robot.pk)

    def withdraw(self, send=None, destination=OTHER_PUBKEY, payment_status=None):
        payment_hash = uuid.uuid4().hex + uuid.uuid4().hex
        validate = {
            "valid": True,
            "context": None,
            "description": "",
            "payment_hash": payment_hash,
            "created_at": timezone.now(),
            "expires_at": timezone.now() + timedelta(hours=1),
        }
        decoded = MagicMock(destination=destination)
        send = send or stream(error=SELF_PAY)
        with (
            patch("api.logics.LNNode.validate_ln_invoice", return_value=validate),
            patch("api.logics.LNNode.decode_payreq", return_value=decoded),
            patch("api.logics.LNNode.get_identity_pubkey", return_value=OWN_PUBKEY),
            patch.object(LNDNode, "payment_status", return_value=payment_status),
            router_with(send) as router,
        ):
            valid, ctx = Logics.withdraw_rewards(self.user, "lnbc7500n1test", 0)
        return valid, ctx, payment_hash, router

    # 1. pre-check: invoice of the coordinator's own node
    def test_own_node_invoice_refused_before_debit(self):
        valid, ctx, payment_hash, router = self.withdraw(destination=OWN_PUBKEY)
        self.assertFalse(valid)
        self.assertEqual(ctx["bad_invoice"], SELF_INVOICE_ERROR)
        router.assert_not_called()
        self.assertFalse(LNPayment.objects.filter(payment_hash=payment_hash).exists())
        self.assertEqual(self.robot().earned_rewards, 750)
        self.assertEqual(ledger_backed(self.user.robot.pk), (750, 0))

    # 2. backstop: LND refuses the self-payment (pre-check could not tell)
    def test_self_payment_error_refunds_with_clear_error(self):
        with patch.dict(os.environ, {"REWARD_WITHDRAW_DELAY": "3600"}):
            valid, ctx, payment_hash, _ = self.withdraw(destination=None)
        self.assertFalse(valid)
        self.assertEqual(ctx["bad_invoice"], SELF_INVOICE_ERROR)
        lnp = LNPayment.objects.get(payment_hash=payment_hash)
        self.assertEqual(lnp.status, LNPayment.Status.FAILRO)
        self.assertEqual(lnp.failure_reason, LNPayment.FailureReason.INCORRE)
        robot = self.robot()
        self.assertEqual(robot.earned_rewards, 750)
        self.assertEqual(robot.claimed_rewards, 0)
        # the refund is withdrawable at once (not delayed like a new reward)
        self.assertEqual(ledger_backed(robot.pk), (750, 0))

    def test_retry_after_self_payment_is_possible(self):
        self.withdraw(destination=None)
        valid, ctx, _, _ = self.withdraw(destination=OWN_PUBKEY)
        self.assertEqual(ctx["bad_invoice"], SELF_INVOICE_ERROR)
        self.assertEqual(self.robot().earned_rewards, 750)

    def test_expired_invoice_preflight_refunds(self):
        err = FakeRpcError(grpc.StatusCode.UNKNOWN, "invoice expired. Valid until 2026-11-18")
        valid, ctx, payment_hash, _ = self.withdraw(send=stream(error=err))
        self.assertFalse(valid)
        self.assertIn("The invoice has expired", ctx["bad_invoice"])
        self.assertEqual(
            LNPayment.objects.get(payment_hash=payment_hash).status,
            LNPayment.Status.FAILRO,
        )
        self.assertEqual(self.robot().earned_rewards, 750)

    # 3. ambiguity guard: the payment may exist → no refund
    def test_error_after_stream_started_is_not_refunded(self):
        err = FakeRpcError(grpc.StatusCode.UNKNOWN, "self-payments not allowed")
        valid, ctx, payment_hash, _ = self.withdraw(
            send=stream([IN_FLIGHT], err), payment_status=LNPayment.Status.FLIGHT
        )
        self.assertFalse(valid)
        self.assertIn("uncertain", ctx["bad_invoice"])
        self.assertEqual(
            LNPayment.objects.get(payment_hash=payment_hash).status,
            LNPayment.Status.FLIGHT,
        )
        self.assertEqual(self.robot().earned_rewards, 0)

    def test_transport_error_unknown_outcome_is_not_refunded(self):
        err = FakeRpcError(grpc.StatusCode.UNAVAILABLE, "Socket closed")
        valid, ctx, payment_hash, _ = self.withdraw(
            send=stream(error=err), payment_status=None
        )
        self.assertFalse(valid)
        self.assertIn("uncertain", ctx["bad_invoice"])
        self.assertEqual(
            LNPayment.objects.get(payment_hash=payment_hash).status,
            LNPayment.Status.VALIDI,
        )
        self.assertEqual(self.robot().earned_rewards, 0)

    def test_already_exists_is_never_treated_as_preflight(self):
        err = FakeRpcError(grpc.StatusCode.ALREADY_EXISTS, "invoice is already paid")
        valid, ctx, payment_hash, _ = self.withdraw(
            send=stream(error=err), payment_status=LNPayment.Status.SUCCED
        )
        self.assertTrue(valid)
        self.assertEqual(
            LNPayment.objects.get(payment_hash=payment_hash).status,
            LNPayment.Status.SUCCED,
        )
        self.assertEqual(self.robot().earned_rewards, 0)
        self.assertEqual(self.robot().claimed_rewards, 750)

    def test_transport_error_that_lnd_reports_failed_is_refunded(self):
        err = FakeRpcError(grpc.StatusCode.UNAVAILABLE, "Socket closed")
        valid, ctx, payment_hash, _ = self.withdraw(
            send=stream([IN_FLIGHT], err), payment_status=LNPayment.Status.FAILRO
        )
        self.assertFalse(valid)
        self.assertEqual(self.robot().earned_rewards, 750)

    def test_unexpected_exception_never_escapes(self):
        valid, ctx, payment_hash, _ = self.withdraw(
            send=stream(error=RuntimeError("boom")), payment_status=None
        )
        self.assertFalse(valid)
        self.assertEqual(self.robot().earned_rewards, 0)


class PreflightClassifierTest(TestCase):
    def test_classification(self):
        pf = LNDNode.preflight_failure
        self.assertEqual(pf(SELF_PAY), SELF_INVOICE_ERROR)
        self.assertIsNotNone(
            pf(FakeRpcError(grpc.StatusCode.UNKNOWN, "invalid payment request: checksum failed"))
        )
        self.assertIsNone(pf(FakeRpcError(grpc.StatusCode.ALREADY_EXISTS, "payment is in transition")))
        self.assertIsNone(pf(FakeRpcError(grpc.StatusCode.UNAVAILABLE, "self-payments not allowed")))
        self.assertIsNone(pf(FakeRpcError(grpc.StatusCode.UNKNOWN, "some db error")))
        self.assertIsNone(pf(RuntimeError("self-payments not allowed")))


class BuyerPayoutPreflightTest(TestCase):
    """follow_send_payment: a request LND rejects before creating the payment is
    failed like a FAILED payment (FAILRO, order FAI), not left to loop."""

    def run_follow(self, error):
        lnp = MagicMock()
        lnp.payment_hash = "ab" * 32
        lnp.invoice = "lnbc10u1test"
        lnp.num_satoshis = 1000
        lnp.routing_attempts = 0
        order = lnp.order_paid_LN
        order.trade_escrow.num_satoshis = 2000
        order.transition_status.return_value = True
        order.t_to_expire.return_value = 60
        with router_with(stream(error=error)):
            result = LNDNode.follow_send_payment(lnp, 10, 30)
        return lnp, order, result

    def test_preflight_rejection_fails_payout(self):
        err = FakeRpcError(grpc.StatusCode.UNKNOWN, "invalid payment request")
        lnp, order, result = self.run_follow(err)
        self.assertEqual(lnp.status, LNPayment.Status.FAILRO)
        self.assertEqual(lnp.routing_attempts, 1)
        order.update_status.assert_called_with(Order.Status.FAI)
        self.assertFalse(result["succeded"])

    def test_transport_error_is_left_for_retry(self):
        err = FakeRpcError(grpc.StatusCode.UNAVAILABLE, "Socket closed")
        lnp, order, result = self.run_follow(err)
        self.assertIsNone(result)
        order.update_status.assert_not_called()
