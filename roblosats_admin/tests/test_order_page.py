import re
from pathlib import Path

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from api.models import LNPayment, OnchainPayment, Order
from roblosats_admin import disputes, orders
from roblosats_admin.tests.test_disputes import make_user
from roblosats_admin.tests.test_orders_alerts import LANGS, make_order

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "roblosats_admin"
LOCALE = Path(__file__).resolve().parent.parent / "locale"


class OrderPageDetailsTest(TestCase):
    """What the order page shows beyond the basics (rl27)."""

    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)

    def page(self, order, lang="en"):
        response = self.client.get(
            reverse("admin:api_order_summary", args=[order.pk]),
            HTTP_ACCEPT_LANGUAGE=lang,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_resolved_dispute_says_who_won(self):
        # SELL order: the maker sells, the taker buys.
        order = make_order(self.maker, self.taker, Order.Status.MLD)
        body = self.page(order).content.decode()
        self.assertIn("Dispute resolved: the maker lost (the buyer won)", body)
        order = make_order(self.maker, self.taker, Order.Status.TLD)
        body = self.page(order).content.decode()
        self.assertIn("Dispute resolved: the taker lost (the seller won)", body)
        # BUY order: the maker buys, so the taker losing means the buyer won.
        Order.objects.filter(pk=order.pk).update(type=Order.Types.BUY)
        body = self.page(order).content.decode()
        self.assertIn("(the buyer won)", body)
        body = self.page(order, "es").content.decode()
        self.assertIn("ganó el comprador", body)

    def test_expiry_reason_and_deadline_only_while_live(self):
        order = make_order(self.maker, self.taker, Order.Status.EXP, expiry_reason=2)
        response = self.page(order)
        self.assertIn(
            "Expired (the seller&#x27;s escrow was not locked)",
            response.content.decode(),
        )
        self.assertIsNone(response.context["deadline"])
        live = make_order(
            self.maker,
            self.taker,
            Order.Status.CHA,
            contract_finalization_time=timezone.now(),
        )
        deadline = self.page(live).context["deadline"]
        self.assertIsNotNone(deadline)
        self.assertFalse(deadline["expired"])
        self.assertTrue(self.page(live).context["contract_time"])

    def test_earnings_routing_fee_and_payment_links(self):
        order = make_order(self.maker, self.taker, Order.Status.SUC, proceeds=1_234)
        LNPayment.objects.filter(pk=order.payout_id).update(
            status=LNPayment.Status.SUCCED, fee=12.5
        )
        response = self.page(order)
        body = response.content.decode()
        self.assertIn("Coordinator's earnings", body)
        self.assertIn(disputes.fmt_sats(1_234) + " sats", body)
        self.assertIn("Routing fee of the payment to the buyer: 12.5 sats.", body)
        for pk in (
            order.maker_bond_id,
            order.taker_bond_id,
            order.trade_escrow_id,
            order.payout_id,
        ):
            self.assertIn(reverse("admin:api_lnpayment_change", args=[pk]), body)
        self.assertIn("Payment: Buyer's payout invoice", body)
        self.assertIn(
            reverse("admin:api_robot_change", args=[self.taker.robot.pk]), body
        )
        body = self.page(order, "es").content.decode()
        self.assertIn("Ganancia del coordinador", body)
        self.assertIn("Comisión de ruta del pago al comprador: 12.5 sats.", body)

    def test_no_routing_fee_before_paying(self):
        order = make_order(self.maker, self.taker, Order.Status.CHA)
        response = self.page(order)
        self.assertEqual(response.context["routing_fee"], "")
        self.assertNotIn("Routing fee", response.content.decode())

    def test_chat_count_who_and_when(self):
        from chat.models import Message

        order = make_order(self.maker, self.taker, Order.Status.CHA)
        start = timezone.now() - timezone.timedelta(hours=1)
        senders = (self.maker, self.taker, self.taker)
        for index, sender in enumerate(senders, start=1):
            Message.objects.create(
                order=order,
                index=index,
                sender=sender,
                receiver=self.maker if sender == self.taker else self.taker,
                created_at=start + timezone.timedelta(minutes=index),
            )
        response = self.page(order)
        chat = response.context["chat"]
        self.assertEqual(chat["total"], 3)
        self.assertEqual(response.context["chat_count"], 3)
        counts = {c["who"]: c["n"] for c in chat["by_who"]}
        # SELL order: maker = seller, taker = buyer.
        self.assertEqual(counts, {"seller (MakerRobot)": 1, "buyer (TakerRobot)": 2})
        self.assertEqual([m["index"] for m in chat["rows"]], [1, 2, 3])
        self.assertLess(chat["first"], chat["last"])
        body = response.content.decode()
        self.assertIn('id="chat"', body)
        self.assertIn("a copy of every message reaches the coordinator", body)
        self.assertIn("When each one wrote", body)

    def test_no_chat(self):
        order = make_order(self.maker, self.taker, Order.Status.CHA)
        body = self.page(order).content.decode()
        self.assertIn("No chat messages are stored on the coordinator.", body)

    def test_on_chain_payout(self):
        order = make_order(self.maker, self.taker, Order.Status.SUC)
        tx = OnchainPayment.objects.create(
            status=OnchainPayment.Status.CONFI,
            address="bc1qexampleaddress",
            txid="ab" * 32,
            num_satoshis=98_500,
            mining_fee_sats=420,
            receiver=self.taker,
            balance=None,
        )
        Order.objects.filter(pk=order.pk).update(
            is_swap=True, payout_tx=tx, payout=None
        )
        response = self.page(order)
        body = response.content.decode()
        self.assertIn(disputes.fmt_sats(98_500), body)
        self.assertIn("confirmed", body)
        self.assertIn("bc1qexampleaddress", body)
        self.assertIn("ab" * 32, body)
        self.assertIn("Mining fee", body)
        self.assertIn(reverse("admin:api_onchainpayment_change", args=[tx.pk]), body)
        self.assertEqual(response.context["routing_fee"], "")
        body = self.page(order, "de").content.decode()
        self.assertIn("bestätigt", body)

    def test_nobody_has_no_robot_stats(self):
        order = make_order(self.maker, self.taker, Order.Status.PUB)
        Order.objects.filter(pk=order.pk).update(
            taker=None, taker_bond=None, trade_escrow=None, payout=None
        )
        body = self.page(order).content.decode()
        self.assertEqual(body.count("Your messages reach them via"), 1)

    def test_every_language_renders_the_full_page(self):
        order = make_order(self.maker, self.taker, Order.Status.MLD, proceeds=500)
        english = None
        for lang in LANGS:
            self.client.cookies["django_language"] = lang
            response = self.client.get(
                reverse("admin:api_order_summary", args=[order.pk])
            )
            self.assertEqual(response.status_code, 200, lang)
            body = response.content.decode()
            note = re.search(
                r'<section class="rsd-sec" id="chat">\s*<h2>(.*?)</h2>', body
            )
            self.assertIsNotNone(note, lang)
            if lang == "en":
                english = note.group(1)
                self.assertEqual(english, "Chat between them")
            else:
                self.assertNotEqual(note.group(1), english, lang)


class TemplateCatalogTest(TestCase):
    def test_order_page_strings_are_in_the_catalog(self):
        po = (LOCALE / "en" / "LC_MESSAGES" / "django.po").read_text("utf-8")
        known = set(re.findall(r'msgctxt "roblosats-admin"\nmsgid "(.*)"', po))
        missing = []
        for name in ("order.html", "_party_summary.html", "_payment_row.html"):
            text = (TEMPLATES / name).read_text("utf-8")
            for msgid in re.findall(
                r'{% translate "([^"]+)" context "roblosats-admin" %}', text
            ):
                if msgid not in known:
                    missing.append((name, msgid))
            for body in re.findall(
                r"{% blocktranslate trimmed context \"roblosats-admin\"[^%]*%}(.*?)"
                r"{% endblocktranslate %}",
                text,
                re.S,
            ):
                msgid = re.sub(r"{{ (\w+) }}", r"%(\1)s", " ".join(body.split()))
                if msgid.replace('"', '\\"') not in known:
                    missing.append((name, msgid))
        self.assertEqual(missing, [])

    def test_python_strings_are_in_the_catalog(self):
        po = (LOCALE / "en" / "LC_MESSAGES" / "django.po").read_text("utf-8")
        known = set(re.findall(r'msgctxt "roblosats-admin"\nmsgid "(.*)"', po))
        text = Path(orders.__file__).read_text("utf-8")
        used = re.findall(r'\b_(?:lazy)?\("([^"]+)"\)', text)
        self.assertTrue(used)
        self.assertEqual([m for m in used if m not in known], [])
