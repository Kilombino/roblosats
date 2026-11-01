import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from api.models import LNPayment, Notification, Order
from roblosats_admin import disputes, messaging

NOW = timezone.now


def make_user(name):
    return User.objects.create_user(username=name, password="x")


def make_payment(sender, concept, sats, status=LNPayment.Status.LOCKED, receiver=None):
    return LNPayment.objects.create(
        type=LNPayment.Types.HOLD
        if concept != LNPayment.Concepts.PAYBUYER
        else LNPayment.Types.NORM,
        concept=concept,
        status=status,
        payment_hash=uuid.uuid4().hex,
        preimage=uuid.uuid4().hex,
        num_satoshis=sats,
        created_at=NOW(),
        expires_at=NOW() + timezone.timedelta(hours=24),
        sender=sender,
        receiver=receiver,
    )


def make_disputed_order(
    order_type,
    maker,
    taker,
    status=Order.Status.DIS,
    escrow_status=LNPayment.Status.SETLED,
):
    buyer, seller = (maker, taker) if order_type == Order.Types.BUY else (taker, maker)
    maker_bond = make_payment(maker, LNPayment.Concepts.MAKEBOND, 3_000)
    taker_bond = make_payment(taker, LNPayment.Concepts.TAKEBOND, 4_000)
    escrow = make_payment(
        seller, LNPayment.Concepts.TRESCROW, 100_000, status=escrow_status
    )
    payout = make_payment(
        None,
        LNPayment.Concepts.PAYBUYER,
        99_000,
        status=LNPayment.Status.VALIDI,
        receiver=buyer,
    )
    logs = [
        {
            "timestamp": NOW().replace(microsecond=0).isoformat(),
            "level": "INFO",
            "event": f"Dispute was opened by Robot({taker.robot.id},{taker.username})",
        }
    ]
    statement = {
        "statement": "I paid, contact me at alice@example.org",
        "messages": [
            {
                "index": 1,
                "plainTextMessage": "hello",
                "validSignature": True,
                "userNick": maker.username,
                "time": NOW().isoformat(),
            },
            {
                "index": 2,
                "plainTextMessage": "paid",
                "validSignature": True,
                "userNick": taker.username,
                "time": NOW().isoformat(),
            },
        ],
    }
    return Order.objects.create(
        type=order_type,
        status=status,
        is_disputed=True,
        created_at=NOW(),
        expires_at=NOW() + timezone.timedelta(hours=10),
        has_range=False,
        amount=50,
        satoshis=100_000,
        t0_satoshis=100_000,
        last_satoshis=100_000,
        payment_method="SEPA",
        maker=maker,
        taker=taker,
        maker_bond=maker_bond,
        taker_bond=taker_bond,
        trade_escrow=escrow,
        payout=payout,
        bond_size=3,
        taker_statement=json.dumps(statement),
        logs=json.dumps(logs),
    )


class RoleMappingTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")

    def test_buy_order_maker_is_buyer(self):
        order = make_disputed_order(Order.Types.BUY, self.maker, self.taker)
        r = disputes.roles(order)
        self.assertEqual(r["buyer"], self.maker)
        self.assertEqual(r["seller"], self.taker)
        self.assertEqual(r["buyer_bond"], order.maker_bond)
        self.assertEqual(disputes.action_for(order, "buyer"), "maker_wins")
        self.assertEqual(disputes.action_for(order, "seller"), "taker_wins")

    def test_sell_order_taker_is_buyer(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        r = disputes.roles(order)
        self.assertEqual(r["buyer"], self.taker)
        self.assertEqual(r["seller"], self.maker)
        self.assertEqual(r["seller_bond"], order.maker_bond)
        self.assertEqual(disputes.action_for(order, "buyer"), "taker_wins")
        self.assertEqual(disputes.action_for(order, "seller"), "maker_wins")
        self.assertEqual(disputes.action_for(order, "return"), "return_everything")
        self.assertEqual(disputes.action_for(order, "completed"), "successful_trade")

    def test_plan_amounts_follow_roles(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        buyer_wins = disputes.plan(order, "buyer")
        self.assertTrue(buyer_wins["possible"])
        self.assertIn("TakerRobot", buyer_wins["lines"][0])
        self.assertIn(disputes.fmt_sats(4_000 + 99_000), buyer_wins["lines"][0])
        seller_wins = disputes.plan(order, "seller")
        self.assertIn("MakerRobot", seller_wins["lines"][0])
        self.assertIn(disputes.fmt_sats(3_000 + 100_000), seller_wins["lines"][0])

    def test_plan_refuses_unsettled_escrow_and_closed_orders(self):
        order = make_disputed_order(
            Order.Types.BUY,
            self.maker,
            self.taker,
            escrow_status=LNPayment.Status.LOCKED,
        )
        self.assertFalse(disputes.plan(order, "seller")["possible"])
        order = make_disputed_order(
            Order.Types.BUY, self.maker, self.taker, status=Order.Status.MLD
        )
        self.assertFalse(disputes.plan(order, "buyer")["possible"])


class CoordinatorMessageTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.order = make_disputed_order(Order.Types.BUY, self.maker, self.taker)

    @patch("roblosats_admin.messaging.coordinator_alias", return_value="TestCoord")
    @patch("roblosats_admin.tasks.relay_coordinator_message.delay")
    @patch("api.tasks.nostr_send_notification_event.delay")
    def test_long_message_is_split_and_relayed(self, nostr, relay, _alias):
        robot = self.maker.robot
        robot.nostr_pubkey = "a" * 64
        robot.telegram_enabled = True
        robot.save()
        text = "x" * 450
        full = messaging.send_coordinator_message(self.order, robot, text)

        head = f"TestCoord coordinator (order {self.order.id}): "
        self.assertEqual(full, head + "x" * messaging.MESSAGE_MAX)
        n = Notification.objects.get(robot=robot, order=self.order)
        self.assertEqual(len(n.title), 240)
        self.assertEqual(n.title + n.description, full[:480])
        nostr.assert_called_once_with(
            robot_id=robot.id, order_id=self.order.id, text=full
        )
        relay.assert_called_once_with(
            robot_id=robot.id, order_id=self.order.id, text=full
        )

    @patch("roblosats_admin.tasks.relay_coordinator_message.delay")
    @patch("api.tasks.nostr_send_notification_event.delay")
    def test_app_only_robot_gets_just_the_notification(self, nostr, relay):
        messaging.send_coordinator_message(self.order, self.taker.robot, "hi")
        self.assertEqual(Notification.objects.filter(robot=self.taker.robot).count(), 1)
        nostr.assert_not_called()
        relay.assert_not_called()

    @patch("roblosats_admin.messaging.coordinator_alias", return_value="TestCoord")
    def test_sent_messages_are_listed_once_per_text(self, _alias):
        with patch("api.tasks.nostr_send_notification_event.delay"):
            messaging.send_coordinator_message(
                self.order, self.maker.robot, "same text"
            )
            messaging.send_coordinator_message(
                self.order, self.taker.robot, "same text"
            )
        rows = disputes.sent_messages(
            self.order, {self.maker.robot.id: "buyer", self.taker.robot.id: "seller"}
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "same text")


class DisputePageTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.page = reverse("admin:api_order_dispute", args=[self.order.pk])

    def test_pages_render(self):
        response = self.client.get(self.page, HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Buyer wins", body)
        self.assertIn("TakerRobot", body)
        self.assertIn("alice@example.org", body)
        self.assertIn("csrfmiddlewaretoken", body)

        response = self.client.get(reverse("admin:api_order_disputes"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.page, response.content.decode())

        response = self.client.get(
            reverse("admin:api_order_change", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.page, response.content.decode())

    def test_confirmation_shows_exact_amounts(self):
        response = self.client.get(
            self.page + "?confirm=buyer", HTTP_ACCEPT_LANGUAGE="en"
        )
        body = response.content.decode()
        self.assertIn('name="confirmed" value="yes"', body)
        self.assertIn(disputes.fmt_sats(4_000 + 99_000), body)

    def test_language_follows_browser_and_selector(self):
        response = self.client.get(self.page, HTTP_ACCEPT_LANGUAGE="ca,es;q=0.8")
        self.assertIn("Gana el comprador", response.content.decode())
        response = self.client.get(self.page + "?lang=de")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies["django_language"].value, "de")
        response = self.client.get(self.page, HTTP_ACCEPT_LANGUAGE="es")
        self.assertIn("Käufer gewinnt", response.content.decode())

    def test_every_language_renders(self):
        for lang in ("en", "es", "de", "fr", "it", "pt", "ru", "zh-hans", "ja"):
            self.client.cookies["django_language"] = lang
            response = self.client.get(self.page + "?confirm=return")
            self.assertEqual(response.status_code, 200, lang)
            self.assertEqual(response["Content-Language"], lang)

    @patch("roblosats_admin.disputes.send_coordinator_message")
    def test_message_to_buyer_only(self, send):
        response = self.client.post(
            self.page,
            {"action": "message", "recipient": "buyer", "text": "Please upload proof"},
        )
        self.assertEqual(response.status_code, 302)
        send.assert_called_once()
        order, robot, text = send.call_args.args
        self.assertEqual(robot, self.taker.robot)
        self.assertEqual(text, "Please upload proof")

    @patch("roblosats_admin.disputes.send_coordinator_message")
    def test_message_rejects_empty_and_too_long(self, send):
        self.client.post(
            self.page, {"action": "message", "recipient": "both", "text": "  "}
        )
        self.client.post(
            self.page, {"action": "message", "recipient": "both", "text": "x" * 401}
        )
        self.client.post(
            self.page, {"action": "message", "recipient": "nobody", "text": "hi"}
        )
        send.assert_not_called()

    @patch("roblosats_admin.disputes.resolve")
    def test_resolve_requires_confirmation_and_current_status(self, resolve):
        self.client.post(
            self.page,
            {
                "action": "resolve",
                "decision": "buyer",
                "status": str(self.order.status),
            },
        )
        self.client.post(
            self.page,
            {
                "action": "resolve",
                "decision": "buyer",
                "confirmed": "yes",
                "status": "10",
            },
        )
        self.client.post(
            self.page,
            {
                "action": "resolve",
                "decision": "steal",
                "confirmed": "yes",
                "status": str(self.order.status),
            },
        )
        resolve.assert_not_called()

        resolve.return_value = (True, ["ok"])
        self.client.post(
            self.page,
            {
                "action": "resolve",
                "decision": "buyer",
                "confirmed": "yes",
                "status": str(self.order.status),
            },
        )
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[2], "buyer")

    @patch("roblosats_admin.disputes.resolve")
    def test_csrf_is_enforced(self, resolve):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        response = client.post(
            self.page,
            {
                "action": "resolve",
                "decision": "buyer",
                "confirmed": "yes",
                "status": str(self.order.status),
            },
        )
        self.assertEqual(response.status_code, 403)
        resolve.assert_not_called()

    def test_non_staff_cannot_open_it(self):
        client = Client()
        client.force_login(self.maker)
        response = client.get(self.page)
        self.assertNotEqual(response.status_code, 200)

    @patch("api.tasks.send_notification.delay")
    @patch("api.admin._credit")
    def test_buyer_wins_runs_the_admin_action_on_a_sell_order(self, credit, _notify):
        response = self.client.post(
            self.page,
            {
                "action": "resolve",
                "decision": "buyer",
                "confirmed": "yes",
                "status": str(self.order.status),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.MLD)
        credit.assert_called_once()
        robot, sats, order, what = credit.call_args.args
        self.assertEqual(robot, self.taker.robot)
        self.assertEqual(sats, 4_000 + 99_000)
        self.assertEqual(what, "taker_wins")

    @patch("roblosats_admin.messaging.send_coordinator_message")
    def test_changelist_action_messages_the_maker(self, send):
        changelist = reverse("admin:api_order_changelist")
        response = self.client.post(
            changelist,
            {
                "action": "message_parties",
                "_selected_action": [self.order.pk],
                "send_message": "1",
                "who": "maker",
                "text": "hello",
            },
        )
        self.assertEqual(response.status_code, 302)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], self.maker.robot)
