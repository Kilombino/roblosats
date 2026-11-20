"""0.9.9: legacy receive addresses (1…, 3…) seen from the coordinator: the order and dispute
pages list the traders' payout/refund addresses and the release outputs whole, with their type
and an explorer link, and the Telegram alerts name them."""

from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from api.escrow.addresses import script_to_address, script_type
from api.escrow.taproot import Transaction, TxIn, TxOut
from api.models import OnchainEscrow, Order
from roblosats_admin import alerts, onchain  # noqa: F401
from roblosats_admin.disputes import fmt_sats
from roblosats_admin.tests.test_disputes import make_user
from roblosats_admin.tests.test_orders_alerts import make_order

P2PKH = "76a91477bff20c60e522dfaa3350c39b030a5d004e839a88ac"  # 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2
P2SH = "a914b472a266d0bd89c13706a4132ccfb16f7c3b9fcb87"  # 3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy
ESCROW_SPK = "5120" + "77" * 32


class Addresses(TestCase):
    def test_script_to_address(self):
        self.assertEqual(script_to_address(bytes.fromhex(P2PKH), "bc"), "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2")
        self.assertEqual(script_to_address(bytes.fromhex(P2SH), "bc"), "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy")
        self.assertEqual(
            script_to_address(bytes.fromhex("0014751e76e8199196d454941c45d1b3a323f1433bd6"), "bc"),
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        )
        self.assertTrue(script_to_address(bytes.fromhex(P2PKH), "bcrt").startswith(("m", "n")))
        self.assertTrue(script_to_address(bytes.fromhex(P2SH), "bcrt").startswith("2"))
        self.assertEqual([script_type(bytes.fromhex(x)) for x in (P2PKH, P2SH)], ["P2PKH", "P2SH"])


class LegacyOnTheCoordinatorPages(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        for target, value in (("roblosats_admin.onchain.tip", 1000), ("api.escrow.flow.mempool_seen", []),
                              ("api.escrow.flow.fee_hint", None), ("roblosats_admin.onchain._hrp", "bc"),
                              ("roblosats_admin.onchain.status_text", "")):
            p = mock.patch(target, return_value=value) if not target.endswith("_hrp") else mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.order = make_order(self.maker, self.taker, Order.Status.SUC, escrow_type=Order.EscrowTypes.ONCHAIN)
        release = Transaction([TxIn("ab" * 32, 0)], [TxOut(1_029_000, bytes.fromhex(P2PKH)), TxOut(30_000, bytes.fromhex(P2SH))])
        deposit = Transaction([TxIn("cd" * 32, 0)], [TxOut(1_060_000, bytes.fromhex(ESCROW_SPK)), TxOut(5_000, bytes.fromhex(P2SH))])
        self.escrow = OnchainEscrow.objects.create(
            order=self.order, status=OnchainEscrow.Status.RELEASED, escrow_sats=1_060_000, rescue_csv=2016,
            address="bc1pescrow", script_pubkey=ESCROW_SPK, funding_txid="cd" * 32, release_txid="ef" * 32,
            funding_tx=deposit.serialize().hex(), release_tx=release.serialize().hex(),
            funding_signatures={"fee_plan": 2, "rescue_plan": 2, "extra_plan": 1,
                                "buyer_payout_spk": P2PKH, "seller_payout_spk": P2SH, "seller_change_to_payout": True,
                                "plan": {"seller_fee": 100, "buyer_fee": 900}},
        )

    def test_order_page_lists_legacy_addresses_whole_with_links(self):
        with mock.patch("api.escrow.flow.coordinator_fee_spk", return_value=bytes.fromhex("5120" + "99" * 32)):
            body = self.client.get(reverse("admin:api_order_summary", args=[self.order.pk]),
                                   HTTP_ACCEPT_LANGUAGE="en").content.decode()
        for addr in ("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"):
            self.assertIn(f"https://mempool.kilombino.com/address/{addr}", body)
            self.assertIn(f">{addr}</code>", body)  # whole, never cut
        for text in ("P2PKH", "P2SH", "buyer payout", "seller refund", fmt_sats(1_029_000), fmt_sats(30_000), fmt_sats(5_000),
                     "its deposit change comes back here", "Release outputs", "Deposit outputs"):
            self.assertIn(text, body)

    def test_alert_names_the_release_addresses(self):
        lines = alerts.onchain_destinations(self.order, {"kind": "release_confirmed"})
        self.assertIn(f"→ buyer payout 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2 (P2PKH): {fmt_sats(1_029_000)} sats", lines)
        self.assertIn(f"→ seller refund 3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy (P2SH): {fmt_sats(30_000)} sats", lines)

    def test_dispute_page_too(self):
        Order.objects.filter(pk=self.order.pk).update(status=Order.Status.DIS)
        with mock.patch("api.escrow.flow.coordinator_fee_spk", return_value=bytes.fromhex("5120" + "99" * 32)):
            r = self.client.get(reverse("admin:api_order_dispute", args=[self.order.pk]), HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("https://mempool.kilombino.com/address/1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", body)
        self.assertIn("P2SH", body)
