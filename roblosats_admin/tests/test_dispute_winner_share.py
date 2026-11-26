"""0.9.16 (Kilombino TG 15674): in a dispute the winner (or the innocent side of a penalty) also
gets SLASHED_BOND_REWARD_SPLIT of the loser's bond, at most of its own bond, as for
cancellations and timeouts. Lightning orders here (both bonds settled when the dispute opened,
as Logics.open_dispute does); hybrid B / mixta C in api/tests/test_roblosats_0912.py and the
regtest suite; onchain escrows already shared it in the release."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from api.models import LNPayment, Order
from roblosats_admin import disputes
from roblosats_admin.tests.test_disputes import make_disputed_order, make_user

S = disputes.fmt_sats


def resolve(client, order, decision):
    return client.post(
        reverse("admin:api_order_dispute", args=[order.pk]),
        {"action": "resolve", "decision": decision, "confirmed": "yes", "status": str(order.status)},
    )


class WinnerShare(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")  # bond 3,000
        self.taker = make_user("TakerRobot")  # bond 4,000
        admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(admin)
        self.p = patch("api.tasks.send_notification.delay")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def _order(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)  # maker sells
        LNPayment.objects.filter(pk__in=[order.maker_bond.pk, order.taker_bond.pk]).update(
            status=LNPayment.Status.SETLED)
        order.refresh_from_db()
        return order

    def _rewards(self):
        self.maker.robot.refresh_from_db()
        self.taker.robot.refresh_from_db()
        return self.maker.robot.earned_rewards, self.taker.robot.earned_rewards

    def test_seller_wins_gets_half_of_the_buyer_bond_capped_at_its_own(self):
        order = self._order()
        plan = " ".join(disputes.plan(order, "seller")["lines"])
        self.assertIn(S(1_500), plan)  # min(4,000, 3,000) * 0.5
        self.assertIn("lost bond", plan)
        # the loser's line says how its bond is split (not "kept by the coordinator")
        self.assertIn(f"their Lightning bond ({S(4_000)}; {S(1_500)} go to the seller as the winner's share "
                      f"and {S(2_500)} to the coordinator)", plan)
        self.assertNotIn("kept by the coordinator", plan)
        self.assertEqual(resolve(self.client, order, "seller").status_code, 302)
        self.assertEqual(self._rewards(), (100_000 + 3_000 + 1_500, 0))

    def test_buyer_wins_gets_half_of_the_seller_bond(self):
        order = self._order()
        self.assertIn(S(1_500), " ".join(disputes.plan(order, "buyer")["lines"]))
        resolve(self.client, order, "buyer")
        self.assertEqual(self._rewards(), (0, 99_000 + 4_000 + 1_500))

    def test_penalize_seller_innocent_buyer_gets_its_share(self):
        order = self._order()
        self.assertIn(S(1_500), " ".join(disputes.plan(order, "penalize_seller")["lines"]))
        resolve(self.client, order, "penalize_seller")
        self.assertEqual(self._rewards(), (100_000, 4_000 + 1_500))

    def test_penalize_buyer_innocent_seller_gets_its_share(self):
        order = self._order()
        resolve(self.client, order, "penalize_buyer")
        self.assertEqual(self._rewards(), (100_000 + 3_000 + 1_500, 0))

    def test_no_share_without_a_loser(self):
        order = self._order()
        self.assertNotIn("lost bond", " ".join(disputes.plan(order, "return")["lines"]))
        resolve(self.client, order, "return")
        self.assertEqual(self._rewards(), (100_000 + 3_000, 4_000))

    def test_an_unsettled_bond_gives_nothing(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)  # bonds still LOCKED
        resolve(self.client, order, "seller")
        self.assertEqual(self._rewards(), (100_000 + 3_000, 0))


class AtAGlance(TestCase):
    """0.9.16: the dispute page opens with what is at stake, where each amount is, who has
    spoken and the next step."""

    def setUp(self):
        self.maker, self.taker = make_user("MakerRobot"), make_user("TakerRobot")
        admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(admin)

    def test_panel_lists_amounts_layers_and_next_step(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        LNPayment.objects.filter(pk=order.maker_bond.pk).update(status=LNPayment.Status.SETLED)
        body = self.client.get(reverse("admin:api_order_dispute", args=[order.pk]),
                               HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertIn("At a glance", body)
        self.assertIn("held by the coordinator (settled)", body)  # the seller's settled bond
        self.assertIn("locked (hold invoice)", body)  # the buyer's bond
        self.assertIn(S(100_000), body)
        self.assertIn("Fiat marked as sent", body)
        self.assertRegex(body, r"Waiting for|Both statements are in")

    def test_panel_is_translated(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        body = self.client.get(reverse("admin:api_order_dispute", args=[order.pk]) + "?lang=es", follow=True,
                               HTTP_ACCEPT_LANGUAGE="es").content.decode()
        self.assertIn("De un vistazo", body)
