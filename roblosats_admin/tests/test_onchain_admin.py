"""On-chain escrow in the coordinator panel (Roblosats 0.9.0): read-only pages render for
every kind of order, the summary and the attention flags."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from api.models import OnchainBond, OnchainEscrow, Order
from roblosats_admin import onchain
from roblosats_admin.tests.test_disputes import make_user
from roblosats_admin.tests.test_orders_alerts import make_order

TXID = "ab" * 32
TXID2 = "cd" * 32


def bond(order, owner, role, status, **fields):
    return OnchainBond.objects.create(
        order=order,
        owner=owner,
        role=role,
        status=status,
        owner_pubkey="02" + "11" * 32,
        coordinator_pubkey="02" + "22" * 32,
        refund_csv=540,
        address="bcrt1pbond" + role,
        script_pubkey="5120" + "33" * 32,
        required_sats=3330,
        **fields,
    )


class OnchainAdminTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        # No node in tests: no chain tip, no mempool.
        patcher = mock.patch("roblosats_admin.onchain.tip", return_value=1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("api.escrow.flow.mempool_seen", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("api.escrow.flow.fee_hint", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.ln = make_order(self.maker, self.taker, Order.Status.SUC)
        self.oc = make_order(self.maker, self.taker, Order.Status.CHA, escrow_type=Order.EscrowTypes.ONCHAIN)
        bond(self.oc, self.maker, "maker", OnchainBond.Status.IN_DEPOSIT, txid=TXID, value=3330, coins=[[TXID, 0, 3330]])
        bond(self.oc, self.taker, "taker", OnchainBond.Status.IN_DEPOSIT, txid=TXID2, value=3330)
        self.oc_escrow = OnchainEscrow.objects.create(
            order=self.oc,
            status=OnchainEscrow.Status.LOCKED,
            escrow_sats=70000,
            funding_txid=TXID,
            confirmed_height=900,
            rescue_csv=2016,
            address="bcrt1pescrow",
            funding_signatures={"fee_plan": 2, "rescue_plan": 2, "extra_plan": 1,
                                "rescue": {"to_seller": 72000, "to_buyer": 4000},
                                "plan": {"seller_fee": 100, "buyer_fee": 900}},
        )
        self.hybrid_a = make_order(
            self.maker, self.taker, Order.Status.WF2, escrow_type=Order.EscrowTypes.ONCHAIN, taker_lightning=True,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        bond(self.hybrid_a, self.maker, "maker", OnchainBond.Status.LOCKED, txid=TXID, value=3330)
        OnchainEscrow.objects.create(
            order=self.hybrid_a, status=OnchainEscrow.Status.FUNDING, rescue_csv=2016,
            funding_signatures={"rescue_plan": 2, "rescue": {"to_seller": 1, "to_buyer": 1}, "seller_rescue_sig": "aa"},
        )
        self.hybrid_b = make_order(self.maker, self.taker, Order.Status.EXP, taker_lightning=True, expiry_reason=3)
        bond(self.hybrid_b, self.maker, "maker", OnchainBond.Status.PENALIZED, txid=TXID, value=3330, spend_txid=TXID2)

    def test_kinds(self):
        self.assertEqual(onchain.kind(self.ln), onchain.LIGHTNING)
        self.assertEqual(onchain.kind(self.oc), onchain.ONCHAIN)
        self.assertEqual(onchain.kind(self.hybrid_a), onchain.HYBRID_A)
        self.assertEqual(onchain.kind(self.hybrid_b), onchain.HYBRID_B)

    def test_order_pages_render_for_each_kind(self):
        for order in (self.ln, self.oc, self.hybrid_a, self.hybrid_b):
            response = self.client.get(reverse("admin:api_order_summary", args=[order.pk]), HTTP_ACCEPT_LANGUAGE="en")
            self.assertEqual(response.status_code, 200, order.pk)
            body = response.content.decode()
            if order == self.ln:
                self.assertNotIn('id="onchain"', body)
            else:
                self.assertIn('id="onchain"', body)
            raw = self.client.get(reverse("admin:api_order_change", args=[order.pk]) + "?raw=1")
            self.assertEqual(raw.status_code, 200, order.pk)
        body = self.client.get(reverse("admin:api_order_summary", args=[self.oc.pk])).content.decode()
        self.assertIn(f"https://mempool.kilombino.com/tx/{TXID}", body)
        self.assertIn("block 2916", body)  # 900 + 2016
        self.assertIn("1916 blocks left", body)

    def test_flags(self):
        flags = onchain.flags(self.hybrid_a, height=1000)
        self.assertTrue(any("deposit window ends" in f for f in flags), flags)
        self.assertTrue(any("rescue signature missing: buyer" in f for f in flags), flags)
        self.assertFalse(any("timelock" in f for f in onchain.flags(self.oc, height=2915)))
        self.assertTrue(any("timelock reached" in f for f in onchain.flags(self.oc, height=2916)))
        self.assertEqual(onchain.flags(self.ln), [])
        self.assertEqual(onchain.flags(self.hybrid_b), [])  # finished

    def test_changelist_summary_filters_and_model_admins(self):
        url = reverse("admin:api_order_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('id="onchain-summary"', body)
        self.assertIn("Needs attention", body)
        summary = response.context["onchain_summary"]
        self.assertEqual(summary["trading"], 2)  # oc (CHA) and hybrid A (WF2)
        self.assertEqual(summary["in_escrow"], onchain.fmt_sats(70000))
        for value, expected in (("ln", self.ln), ("onchain", self.oc), ("hybrid_a", self.hybrid_a), ("hybrid_b", self.hybrid_b)):
            listed = list(self.client.get(url, {"escrow_kind": value}).context["cl"].result_list)
            self.assertEqual([o.pk for o in listed], [expected.pk], value)
        listed = self.client.get(url, {"onchain_status": OnchainEscrow.Status.LOCKED}).context["cl"].result_list
        self.assertEqual([o.pk for o in listed], [self.oc.pk])
        self.assertEqual(self.client.get(url, {"q": TXID2}).status_code, 200)
        for name in ("admin:api_onchainbond_changelist", "admin:api_onchainescrow_changelist"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:api_onchainescrow_change", args=[self.oc_escrow.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:api_onchainbond_add")).status_code, 403)
