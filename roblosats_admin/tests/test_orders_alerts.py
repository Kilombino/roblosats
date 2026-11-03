import os
import re
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse

from api.models import Order
from roblosats_admin import alerts, disputes
from roblosats_admin.models import EVENT_FIELDS, TelegramAlerts
from roblosats_admin.tests.test_disputes import make_disputed_order, make_user

LANGS = ("en", "es", "de", "fr", "it", "pt", "ru", "zh-hans", "ja")
TOKEN = "123456789:AAH" + "x" * 32 + "SECRET"


def make_order(maker, taker, status, **fields):
    order = make_disputed_order(Order.Types.SELL, maker, taker, status=status)
    Order.objects.filter(pk=order.pk).update(
        is_disputed=False, taker_statement=None, **fields
    )
    order.refresh_from_db()
    return order


def enable(**toggles):
    cfg = TelegramAlerts(pk=1, bot_token=TOKEN, chat_id="-100123", **toggles)
    cfg.save()
    return cfg


class OrderPageTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.order = make_order(
            self.maker, self.taker, Order.Status.SUC, spamcoin_sats=4_321_000
        )
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.page = reverse("admin:api_order_summary", args=[self.order.pk])

    def test_renders_any_order(self):
        from chat.models import Message

        for index in (1, 2, 3):
            Message.objects.create(
                order=self.order, index=index, sender=self.maker, receiver=self.taker
            )
        response = self.client.get(self.page, HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Completed successfully", body)
        self.assertIn(disputes.fmt_sats(4_321_000), body)
        self.assertIn("TakerRobot", body)
        self.assertIn("Dispute was opened by", body)
        self.assertIn(
            reverse("admin:api_order_change", args=[self.order.pk]) + "?raw=1", body
        )
        self.assertEqual(response.context["chat_count"], 3)
        self.assertNotIn("Statement:", body)

        response = self.client.get(self.page, HTTP_ACCEPT_LANGUAGE="es")
        body = response.content.decode()
        self.assertIn("Cronología", body)
        self.assertIn("Sats de Spamcoin", body)

    def test_public_order_without_taker(self):
        order = make_order(self.maker, self.taker, Order.Status.PUB)
        Order.objects.filter(pk=order.pk).update(
            taker=None, taker_bond=None, trade_escrow=None, payout=None
        )
        response = self.client.get(
            reverse("admin:api_order_summary", args=[order.pk]),
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("(nobody)", body)
        self.assertIn("Public in the order book", body)
        self.assertNotIn('name="recipient"', body)

    def test_every_language_renders(self):
        for lang in LANGS:
            self.client.cookies["django_language"] = lang
            response = self.client.get(self.page)
            self.assertEqual(response.status_code, 200, lang)
            self.assertEqual(response["Content-Language"], lang)

    def test_change_page_and_list_lead_here(self):
        change = reverse("admin:api_order_change", args=[self.order.pk])
        response = self.client.get(change)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.page)
        self.assertEqual(self.client.get(change + "?raw=1").status_code, 200)
        response = self.client.get(reverse("admin:api_order_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.page, response.content.decode())

    def test_disputed_order_points_to_the_dispute_page(self):
        disputed = make_disputed_order(Order.Types.BUY, self.maker, self.taker)
        response = self.client.get(
            reverse("admin:api_order_summary", args=[disputed.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            reverse("admin:api_order_dispute", args=[disputed.pk]),
            response.content.decode(),
        )
        response = self.client.get(
            reverse("admin:api_order_change", args=[disputed.pk])
        )
        self.assertEqual(
            response["Location"], reverse("admin:api_order_dispute", args=[disputed.pk])
        )

    @patch("roblosats_admin.disputes.send_coordinator_message")
    def test_write_from_a_live_order(self, send):
        order = make_order(self.maker, self.taker, Order.Status.CHA)
        page = reverse("admin:api_order_summary", args=[order.pk])
        self.assertIn('name="recipient"', self.client.get(page).content.decode())
        response = self.client.post(
            page, {"action": "message", "recipient": "seller", "text": "hello"}
        )
        self.assertEqual(response.status_code, 302)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], self.maker.robot)

    def test_non_staff_cannot_open_it(self):
        client = Client()
        client.force_login(self.maker)
        self.assertNotEqual(client.get(self.page).status_code, 200)


class EventMappingTest(TestCase):
    def test_transitions(self):
        S = Order.Status
        cases = {
            (S.WFB, S.PUB): "published",
            (S.WF2, S.PUB): "published",
            (S.PAU, S.PUB): None,
            (S.PUB, S.PAU): None,
            (S.PUB, S.WF2): "taken",
            (S.WF2, S.CHA): "escrow_locked",
            (S.WFE, S.CHA): "escrow_locked",
            (S.FSE, S.CHA): None,
            (S.PAY, S.SUC): "finished",
            (S.PUB, S.UCA): "cancelled",
            (S.PUB, S.EXP): "cancelled",
            (S.WFB, S.EXP): None,
            (S.CHA, S.CCA): "cancelled",
            (S.FSE, S.DIS): "dispute",
            (S.DIS, S.WFR): None,
        }
        for (old, new), event in cases.items():
            self.assertEqual(alerts.event_for(old, new), event, (old, new))

    def test_every_event_has_a_toggle(self):
        self.assertEqual(
            tuple(field for field, _label, _icon in alerts.EVENTS.values()),
            EVENT_FIELDS,
        )


@patch("roblosats_admin.tasks.telegram_alert.delay")
class HookTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")

    def test_status_change_queues_only_enabled_events(self, delay):
        enable(on_finished=True)
        order = make_order(self.maker, self.taker, Order.Status.PAY)
        with self.captureOnCommitCallbacks(execute=True):
            order.update_status(Order.Status.SUC)
        delay.assert_called_once()
        self.assertEqual(delay.call_args.kwargs["event"], "finished")
        self.assertEqual(delay.call_args.kwargs["order_id"], order.pk)

        delay.reset_mock()
        other = make_order(self.maker, self.taker, Order.Status.CHA)
        with self.captureOnCommitCallbacks(execute=True):
            other.update_status(Order.Status.CCA)
        delay.assert_not_called()

    def test_transition_status_and_publish_are_hooked(self, delay):
        from api.logics import Logics

        enable(on_cancelled=True, on_published=True)
        order = make_order(self.maker, self.taker, Order.Status.PUB)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(
                order.transition_status(Order.Status.UCA, [Order.Status.PUB])
            )
        self.assertEqual(delay.call_args.kwargs["event"], "cancelled")

        delay.reset_mock()
        draft = make_order(self.maker, self.taker, Order.Status.WFB)
        with (
            patch("api.logics.nostr_send_order_event.delay"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            Logics.publish_order(draft)
        delay.assert_called_once()
        self.assertEqual(delay.call_args.kwargs["event"], "published")
        self.assertEqual(delay.call_args.kwargs["extra"]["old"], Order.Status.WFB)

    def test_broker_failure_does_not_break_the_order(self, delay):
        delay.side_effect = ConnectionError("redis down")
        enable(on_finished=True)
        order = make_order(self.maker, self.taker, Order.Status.PAY)
        with self.captureOnCommitCallbacks(execute=True):
            order.update_status(Order.Status.SUC)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)

    def test_nothing_configured_queues_nothing(self, delay):
        order = make_order(self.maker, self.taker, Order.Status.FSE)
        empty = {"TELEGRAM_TOKEN": "", "TELEGRAM_COORDINATOR_CHAT_ID": ""}
        with (
            patch.dict(os.environ, empty),
            self.captureOnCommitCallbacks(execute=True),
        ):
            order.update_status(Order.Status.DIS)
        delay.assert_not_called()

    def test_dispute_chat_is_throttled(self, delay):
        from chat.models import Message

        enable(on_dispute_chat=True)
        order = make_disputed_order(Order.Types.BUY, self.maker, self.taker)
        with self.captureOnCommitCallbacks(execute=True):
            Message.objects.create(
                order=order, index=1, sender=self.maker, receiver=self.taker
            )
            Message.objects.create(
                order=order, index=2, sender=self.taker, receiver=self.maker
            )
        delay.assert_called_once()
        self.assertEqual(delay.call_args.kwargs["event"], "dispute_chat")
        self.assertEqual(delay.call_args.kwargs["extra"]["sender"], "MakerRobot")

        delay.reset_mock()
        live = make_order(self.maker, self.taker, Order.Status.CHA)
        with self.captureOnCommitCallbacks(execute=True):
            Message.objects.create(
                order=live, index=1, sender=self.maker, receiver=self.taker
            )
        delay.assert_not_called()

    @patch("api.tasks.nostr_send_notification_event.delay")
    def test_coordinator_message_alert(self, _nostr, delay):
        enable(on_coordinator_message=True)
        order = make_disputed_order(Order.Types.BUY, self.maker, self.taker)
        with self.captureOnCommitCallbacks(execute=True):
            disputes.write_to(order, "both", "please send proof")
        delay.assert_called_once()
        extra = delay.call_args.kwargs["extra"]
        self.assertEqual(extra["names"], ["MakerRobot", "TakerRobot"])
        self.assertEqual(extra["text"], "please send proof")


class EnvironmentFallbackTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")

    def test_env_keeps_dispute_alerts_on(self):
        env = {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_COORDINATOR_CHAT_ID": "42"}
        with patch.dict(os.environ, env):
            cfg = alerts.current()
            self.assertIsNone(TelegramAlerts.objects.filter(pk=1).first())
            self.assertTrue(alerts.wants(cfg, "dispute"))
            self.assertFalse(alerts.wants(cfg, "finished"))
            self.assertEqual(alerts.chat_of(cfg), "42")
            self.assertEqual(alerts.token_of(cfg), TOKEN)
        with patch.dict(
            os.environ, {"TELEGRAM_TOKEN": "", "TELEGRAM_COORDINATOR_CHAT_ID": ""}
        ):
            self.assertFalse(alerts.wants(alerts.current(), "dispute"))

    @patch("api.tasks.nostr_send_notification_event.delay")
    @patch("api.notifications.Notifications.send_telegram_message")
    def test_upstream_does_not_send_the_coordinator_copy(self, send, _nostr):
        from api.notifications import Notifications

        order = make_disputed_order(Order.Types.BUY, self.maker, self.taker)
        env = {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_COORDINATOR_CHAT_ID": "42"}
        with patch.dict(os.environ, env):
            Notifications().dispute_opened(order)
        send.assert_not_called()


class DeliverTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")

    @patch("roblosats_admin.messaging.coordinator_alias", return_value="TestCoord")
    @patch("roblosats_admin.tasks.send_telegram", return_value=True)
    def test_text_language_and_link(self, send, _alias):
        cfg = enable(on_dispute=True, language="es", panel_url="https://coord.example")
        order = make_disputed_order(Order.Types.BUY, self.maker, self.taker)
        self.assertTrue(alerts.deliver("dispute", order.pk, {}))
        _session, chat, text = send.call_args.args
        self.assertEqual(chat, "-100123")
        self.assertEqual(send.call_args.kwargs["token"], TOKEN)
        self.assertIn("TestCoord", text)
        self.assertIn(f"Se ha abierto una disputa en la orden {order.pk}", text)
        self.assertIn(
            "https://coord.example"
            + reverse("admin:api_order_dispute", args=[order.pk]),
            text,
        )

        cfg.language = "en"
        cfg.panel_url = ""
        cfg.on_finished = True
        cfg.save()
        done = make_order(self.maker, self.taker, Order.Status.SUC)
        with patch.dict(os.environ, {"HOST_NAME": "coord.onion"}):
            alerts.deliver("finished", done.pk, {})
        text = send.call_args.args[2]
        self.assertIn(f"Order {done.pk} finished successfully", text)
        self.assertIn(
            "http://coord.onion" + reverse("admin:api_order_summary", args=[done.pk]),
            text,
        )

    @patch("roblosats_admin.tasks.send_telegram")
    def test_disabled_after_queueing_sends_nothing(self, send):
        enable(on_finished=False)
        order = make_order(self.maker, self.taker, Order.Status.SUC)
        self.assertFalse(alerts.deliver("finished", order.pk, {}))
        send.assert_not_called()

    def test_errors_never_reveal_the_token(self):
        class Boom:
            def post(self, url, **kwargs):
                raise ConnectionError(f"failed to reach {url}")

        from roblosats_admin.tasks import telegram_send_once

        ok, detail = telegram_send_once(Boom(), TOKEN, "1", "hi")
        self.assertFalse(ok)
        self.assertNotIn(TOKEN, detail)
        self.assertIn("***", detail)


class AlertsPageTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.page = reverse("admin:roblosats_admin_telegramalerts_changelist")

    def post(self, **data):
        base = {"action": "save", "language": "en", "chat_id": "", "panel_url": ""}
        base.update(data)
        return self.client.post(self.page, base)

    def test_renders_in_every_language(self):
        for lang in LANGS:
            self.client.cookies["django_language"] = lang
            response = self.client.get(self.page)
            self.assertEqual(response.status_code, 200, lang)
        self.client.cookies["django_language"] = "es"
        self.assertIn(
            "Avisos por Telegram", self.client.get(self.page).content.decode()
        )

    def test_save_masks_the_token(self):
        response = self.post(
            bot_token=TOKEN,
            chat_id="-100123",
            language="de",
            panel_url="https://coord.example/",
            on_dispute="1",
            on_finished="1",
        )
        self.assertEqual(response.status_code, 302)
        cfg = TelegramAlerts.objects.get(pk=1)
        self.assertEqual(cfg.bot_token, TOKEN)
        self.assertEqual(cfg.chat_id, "-100123")
        self.assertEqual(cfg.language, "de")
        self.assertEqual(cfg.panel_url, "https://coord.example")
        self.assertTrue(cfg.on_dispute and cfg.on_finished)
        self.assertFalse(cfg.on_published)

        body = self.client.get(self.page, HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertNotIn(TOKEN, body)
        self.assertNotIn(TOKEN[:20], body)
        self.assertIn("…CRET", body)

        self.post(chat_id="-100123")
        self.assertEqual(TelegramAlerts.objects.get(pk=1).bot_token, TOKEN)
        self.post(chat_id="-100123", clear_token="1")
        self.assertEqual(TelegramAlerts.objects.get(pk=1).bot_token, "")

    def test_rejects_bad_values(self):
        self.post(bot_token="not-a-token", chat_id="hello world", on_dispute="1")
        self.assertFalse(TelegramAlerts.objects.filter(pk=1).exists())
        self.post(panel_url="javascript:alert(1)")
        self.assertFalse(TelegramAlerts.objects.filter(pk=1).exists())

    def test_test_button(self):
        with patch(
            "roblosats_admin.tasks.telegram_send_once", return_value=(True, "")
        ) as send:
            response = self.client.post(self.page, {"action": "test"})
            self.assertEqual(response.status_code, 302)
            send.assert_not_called()
            enable()
            self.client.post(self.page, {"action": "test"})
            send.assert_called_once()
            token, chat = send.call_args.args[1:3]
            self.assertEqual((token, chat), (TOKEN, "-100123"))
        with patch(
            "roblosats_admin.tasks.telegram_send_once",
            return_value=(False, "400 Bad Request: chat not found"),
        ):
            response = self.client.post(
                self.page, {"action": "test"}, follow=True, HTTP_ACCEPT_LANGUAGE="en"
            )
            self.assertIn("chat not found", response.content.decode())

    def test_only_with_change_permission(self):
        staff = User.objects.create_user("helper", password="x", is_staff=True)
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.get(self.page).status_code, 403)
        self.assertEqual(client.post(self.page, {"action": "test"}).status_code, 403)
        staff.user_permissions.add(
            Permission.objects.get(codename="change_telegramalerts")
        )
        staff = User.objects.get(pk=staff.pk)
        client.force_login(staff)
        self.assertEqual(client.get(self.page).status_code, 200)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        response = client.post(self.page, {"action": "save", "on_dispute": "1"})
        self.assertEqual(response.status_code, 403)


@patch("roblosats_admin.dashboard._channel_balance", return_value=("1", "2"))
class DashboardTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        maker, taker = make_user("MakerRobot"), make_user("TakerRobot")
        self.live = make_order(maker, taker, Order.Status.CHA)
        make_disputed_order(Order.Types.BUY, maker, taker)

    def test_home_is_translated(self, _balance):
        body = self.client.get(
            "/coordinator/", HTTP_ACCEPT_LANGUAGE="en"
        ).content.decode()
        self.assertIn("Live trades", body)
        self.assertIn("Orders in progress", body)
        self.assertIn("Disputes waiting: 1", body)
        self.assertIn(reverse("admin:api_order_summary", args=[self.live.pk]), body)
        self.assertIn("Telegram alerts", body)
        body = self.client.get(
            "/coordinator/", HTTP_ACCEPT_LANGUAGE="es"
        ).content.decode()
        self.assertIn("Operaciones vivas", body)
        self.assertIn("Órdenes en curso", body)
        self.assertIn("Gente y mercado", body)
        for lang in LANGS:
            self.client.cookies["django_language"] = lang
            self.assertEqual(self.client.get("/coordinator/").status_code, 200, lang)


class CatalogTest(TestCase):
    def test_every_language_has_every_string(self):
        root = Path(__file__).resolve().parent.parent / "locale"
        entry = re.compile(r'msgctxt "roblosats-admin"\nmsgid "(.*)"\nmsgstr "(.*)"')

        def catalog(lang):
            text = (root / lang / "LC_MESSAGES" / "django.po").read_text("utf-8")
            return dict(entry.findall(text))

        english = catalog("en")
        self.assertGreater(len(english), 250)
        for lang in ("es", "de", "fr", "it", "pt", "ru", "zh_Hans", "ja"):
            other = catalog(lang)
            self.assertEqual(set(other), set(english), lang)
            empty = [k for k, v in other.items() if not v]
            self.assertEqual(empty, [], lang)
            for msgid, msgstr in other.items():
                self.assertEqual(
                    sorted(re.findall(r"%\(\w+\)s", msgid)),
                    sorted(re.findall(r"%\(\w+\)s", msgstr)),
                    (lang, msgid),
                )
