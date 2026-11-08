import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from api.models import LNPayment, Order
from roblosats_admin import disputes, payouts
from roblosats_admin.tests.test_disputes import (
    make_disputed_order,
    make_payment,
    make_user,
)

NOW = timezone.now
S = disputes.fmt_sats


def resolve(client, order, decision):
    return client.post(
        reverse("admin:api_order_dispute", args=[order.pk]),
        {
            "action": "resolve",
            "decision": decision,
            "confirmed": "yes",
            "status": str(order.status),
        },
    )


class PenalizeTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)

    def test_penalize_maps_to_the_guilty_side(self):
        sell = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        buy = make_disputed_order(Order.Types.BUY, self.maker, self.taker)
        # SELL: the maker sells. BUY: the maker buys.
        self.assertEqual(disputes.action_for(sell, "penalize_seller"), "penalize_maker")
        self.assertEqual(disputes.action_for(sell, "penalize_buyer"), "penalize_taker")
        self.assertEqual(disputes.action_for(buy, "penalize_seller"), "penalize_taker")
        self.assertEqual(disputes.action_for(buy, "penalize_buyer"), "penalize_maker")

    def test_plan_names_who_keeps_what(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        plan = disputes.plan(order, "penalize_seller")
        self.assertTrue(plan["possible"])
        text = " ".join(plan["lines"])
        self.assertIn("MakerRobot", text)  # the seller on a SELL order
        self.assertIn(S(100_000), text)  # escrow back
        self.assertIn(S(3_000), text)  # seller bond lost
        self.assertIn(S(4_000), text)  # buyer bond back

        closed = make_disputed_order(
            Order.Types.SELL, self.maker, self.taker, escrow_status=LNPayment.Status.LOCKED
        )
        self.assertFalse(disputes.plan(closed, "penalize_buyer")["possible"])

    @patch("api.tasks.send_notification.delay")
    def test_penalize_seller_credits_and_outcome(self, _notify):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        response = resolve(self.client, order, "penalize_seller")
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.MLD)  # the maker (seller) lost

        self.maker.robot.refresh_from_db()
        self.taker.robot.refresh_from_db()
        self.assertEqual(self.maker.robot.earned_rewards, 100_000)  # escrow only
        self.assertEqual(self.taker.robot.earned_rewards, 4_000)  # bond only

        rows = {r["role"]: r for r in disputes.outcome(order)}
        self.assertEqual(rows["buyer"]["received_n"], 4_000)
        self.assertEqual(rows["buyer"]["losses"], [])
        self.assertEqual(rows["seller"]["received_n"], 100_000)
        self.assertEqual([loss["sats"] for loss in rows["seller"]["losses"]], [S(3_000)])
        self.assertFalse(rows["seller"]["all_claimed"])

        body = self.client.get(
            reverse("admin:api_order_dispute", args=[order.pk]),
            HTTP_ACCEPT_LANGUAGE="en",
        ).content.decode()
        self.assertIn("Outcome of the dispute", body)
        self.assertIn("+" + S(100_000) + " sats", body)
        self.assertIn("−" + S(3_000) + " sats", body)

        # Running it twice credits nothing more: the order is no longer in dispute.
        resolve(self.client, order, "penalize_seller")
        self.maker.robot.refresh_from_db()
        self.assertEqual(self.maker.robot.earned_rewards, 100_000)

    @patch("api.tasks.send_notification.delay")
    def test_buyer_wins_outcome_shows_seller_losses(self, _notify):
        order = make_disputed_order(Order.Types.BUY, self.maker, self.taker)
        resolve(self.client, order, "buyer")
        order.refresh_from_db()
        rows = {r["role"]: r for r in disputes.outcome(order)}
        self.assertEqual(rows["buyer"]["received_n"], 3_000 + 99_000)
        self.assertEqual(rows["buyer"]["losses"], [])
        self.assertEqual(rows["seller"]["received_n"], 0)
        self.assertEqual(
            sorted(loss["sats"] for loss in rows["seller"]["losses"]),
            [S(100_000), S(4_000)],
        )

    def test_no_outcome_while_unresolved(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        self.assertEqual(disputes.outcome(order), [])
        body = self.client.get(
            reverse("admin:api_order_dispute", args=[order.pk])
        ).content.decode()
        self.assertNotIn('id="outcome"', body)
        self.assertIn("?confirm=penalize_seller", body)


def failed_order(maker, taker, status=Order.Status.FAI):
    order = make_disputed_order(Order.Types.SELL, maker, taker, status=status)
    order.is_disputed = False
    def payout(status):
        p = make_payment(None, LNPayment.Concepts.PAYBUYER, 99_000, status=status,
                         receiver=taker)
        # real payment hashes are 64 hex chars (the log regex relies on it)
        h = uuid.uuid4().hex * 2
        LNPayment.objects.filter(pk=p.pk).update(payment_hash=h)
        return LNPayment.objects.get(payment_hash=h)

    first = payout(LNPayment.Status.FAILRO)
    current = payout(LNPayment.Status.VALIDI)
    order.payout = current
    t = NOW().replace(microsecond=0)

    def at(minutes):
        return (t + timezone.timedelta(minutes=minutes)).isoformat()

    log = [
        {"timestamp": at(0), "level": "INFO", "event":
            f"Robot({taker.robot.id},{taker.username}) added the invoice "
            f"LNPayment({first.payment_hash},{first.payment_hash}) as payout method. "
            "Amount to be sent is 99000 Sats, routing budget is 99.0 Sats (1000ppm)"},
        {"timestamp": at(1), "level": "INFO", "event":
            f"Payment LNPayment({first.payment_hash},Payment to buyer) **failed**. "
            "Failure reason: FAILURE_REASON_TIMEOUT)"},
        {"timestamp": at(1), "level": "INFO", "event":
            "Order state went from 13: *Sending satoshis to buyer* to 15: "
            "*Failed lightning network routing*"},
        {"timestamp": at(5), "level": "INFO", "event":
            f"Robot({taker.robot.id},{taker.username}) added the invoice "
            f"LNPayment({current.payment_hash},{current.payment_hash}) as payout method. "
            "Amount to be sent is 99000 Sats, routing budget is 99.0 Sats (1000ppm)"},
        {"timestamp": at(6), "level": "INFO", "event":
            f"Payment LNPayment({current.payment_hash},Payment to buyer) **failed**. "
            "Failure reason: FAILURE_REASON_NO_ROUTE)"},
    ]
    order.logs = json.dumps(log)
    order.save()
    return order


@patch("roblosats_admin.payouts._decode", return_value=None)
class FailedPayoutsTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)

    def test_history_comes_from_the_log(self, _decode):
        order = failed_order(self.maker, self.taker)
        hist = payouts.read_history(order)
        self.assertEqual(len(hist["invoices"]), 2)
        self.assertEqual(hist["attempts"], 2)
        self.assertEqual(hist["times_failed"], 1)
        self.assertEqual(hist["invoices"][0]["budget"], "99 sats (1000 ppm)")
        summary = payouts.payout_summary(order, with_invoices=True)
        self.assertEqual(summary["invoices"], 2)
        self.assertEqual(summary["reason"], payouts.reason("no_route"))
        self.assertEqual(sum(1 for r in summary["rows"] if r["current"]), 1)

    def test_reasons(self, _decode):
        self.assertIn("90", payouts.reason("FAILURE_REASON_TIMEOUT"))
        self.assertEqual(payouts.reason(2), payouts.reason("FAILURE_REASON_NO_ROUTE"))
        self.assertEqual(payouts.reason("something new"), "something new")

    def test_pages(self, _decode):
        live = failed_order(self.maker, self.taker)
        done = failed_order(self.maker, self.taker, status=Order.Status.SUC)
        url = reverse("admin:api_order_failed_payouts")

        body = self.client.get(url, HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertIn(reverse("admin:api_order_summary", args=[live.pk]), body)
        self.assertNotIn(reverse("admin:api_order_summary", args=[done.pk]), body)
        self.assertIn("Failing now (1)", body)

        body = self.client.get(url + "?view=history").content.decode()
        self.assertIn(reverse("admin:api_order_summary", args=[done.pk]), body)
        self.assertNotIn(reverse("admin:api_order_summary", args=[live.pk]), body)

        body = self.client.get(
            reverse("admin:api_order_summary", args=[live.pk]), HTTP_ACCEPT_LANGUAGE="en"
        ).content.decode()
        self.assertIn('id="buyer-payout"', body)
        self.assertIn("The payment to the buyer is failing", body)

        body = self.client.get("/coordinator/", HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertIn("Failed payouts: 1", body)

        for lang in ("es", "de", "ja"):
            response = self.client.get(url + f"?lang={lang}", follow=True)
            self.assertEqual(response.status_code, 200)

    def test_destination_is_shown_without_the_invoice(self, decode):
        order = failed_order(self.maker, self.taker)
        node = "02" + "ab" * 32
        hint = "03" + "cd" * 32
        decode.return_value = {"destination": node, "hints": [hint]}
        with patch("roblosats_admin.payouts._alias", return_value="Alias"):
            body = self.client.get(
                reverse("admin:api_order_failed_payouts"), HTTP_ACCEPT_LANGUAGE="en"
            ).content.decode()
        self.assertIn(node, body)  # in the title attribute
        self.assertIn("route hint via", body)
        self.assertNotIn(order.payout.invoice or "lnbc", body)

    def test_non_staff_cannot_open_it(self, _decode):
        client = Client()
        client.force_login(self.maker)
        response = client.get(reverse("admin:api_order_failed_payouts"))
        self.assertNotEqual(response.status_code, 200)
