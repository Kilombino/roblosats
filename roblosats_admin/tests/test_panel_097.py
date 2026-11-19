"""0.9.5–0.9.7 in the coordinator panel: one-signature release, hybrid plan, automatic bond
return (stored, sent, by hand), chain status of releases and payouts, Spamcoin currency,
queued on-chain payouts and the bond_manual Telegram alert."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from api.models import Currency, OnchainBond, OnchainEscrow, OnchainPayment, Order
from roblosats_admin import alerts, disputes, onchain, payouts
from roblosats_admin.models import TelegramAlerts
from roblosats_admin.tests.test_disputes import make_user
from roblosats_admin.tests.test_onchain_admin import TXID, TXID2, bond
from roblosats_admin.tests.test_orders_alerts import TOKEN, make_order

ADDR = "bcrt1qreturnaddressxxxxxxxxxxxxxxxxxxxxxx"


class Panel097Test(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        for target, value in (
            ("roblosats_admin.onchain.tip", 1000),
            ("api.escrow.flow.mempool_seen", []),
            ("api.escrow.flow.fee_hint", None),
            ("api.order_summary.tx_status", "confirmed"),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _escrow(self, order, **kw):
        defaults = dict(status=OnchainEscrow.Status.LOCKED, escrow_sats=70000, funding_txid=TXID,
                        confirmed_height=900, rescue_csv=2016, address="bcrt1pescrow",
                        funding_signatures={"fee_plan": 2, "rescue_plan": 2, "extra_plan": 1})
        defaults.update(kw)
        return OnchainEscrow.objects.create(order=order, **defaults)

    # ------------------------------------------------------------ release path
    def test_leaf_release_shown(self):
        o = make_order(self.maker, self.taker, Order.Status.SUC, escrow_type=Order.EscrowTypes.ONCHAIN)
        self._escrow(o, status=OnchainEscrow.Status.RELEASED, release_txid=TXID2,
                     release_kind=OnchainEscrow.ReleaseKind.COOPERATIVE,
                     funding_signatures={"fee_plan": 2, "rescue_plan": 2, "extra_plan": 1,
                                         "release_path": "seller_leaf"})
        e = onchain.order_section(o)["escrow"]
        self.assertIn("seller", e["release_path"])
        self.assertEqual(e["release_status"], "confirmed")
        r = self.client.get(reverse("admin:api_order_summary", args=[o.pk]))
        self.assertContains(r, "seller signed alone")

    def test_musig_release_shown(self):
        o = make_order(self.maker, self.taker, Order.Status.SUC, escrow_type=Order.EscrowTypes.ONCHAIN)
        self._escrow(o, status=OnchainEscrow.Status.RELEASED, release_txid=TXID2,
                     release_kind=OnchainEscrow.ReleaseKind.COOPERATIVE)
        self.assertIn("MuSig2", onchain.order_section(o)["escrow"]["release_path"])

    def test_waiting_leaf_flag_names_only_the_seller(self):
        o = make_order(self.maker, self.taker, Order.Status.CHA, escrow_type=Order.EscrowTypes.ONCHAIN)
        self._escrow(o, status=OnchainEscrow.Status.RELEASING, release_tx="00",
                     release_kind=OnchainEscrow.ReleaseKind.COOPERATIVE,
                     funding_signatures={"fee_plan": 2, "rescue_plan": 2, "seller_leaf_release": True})
        with mock.patch("api.escrow.service.leaf_release_ok", return_value=True):
            f = onchain.flags(o)
        self.assertTrue(any("one signature" in x for x in f), f)
        self.assertFalse(any("missing" in x for x in f), f)

    def test_hybrid_plan_shown(self):
        o = make_order(self.maker, self.taker, Order.Status.CHA, escrow_type=Order.EscrowTypes.ONCHAIN,
                       taker_lightning=True)
        self._escrow(o, funding_signatures={"fee_plan": 2, "rescue_plan": 1, "hybrid_plan": 1})
        self.assertEqual(onchain.order_section(o)["escrow"]["hybrid_plan"], 1)
        r = self.client.get(reverse("admin:api_order_summary", args=[o.pk]))
        self.assertContains(r, "signs nothing on-chain")

    # ------------------------------------------------------------ automatic bond return
    def test_auto_return_states(self):
        o = make_order(self.maker, self.taker, Order.Status.CHA, escrow_type=Order.EscrowTypes.ONCHAIN)
        b = bond(o, self.maker, "maker", OnchainBond.Status.LOCKED, txid=TXID, value=3330)
        self.assertIn("by hand", onchain.auto_return_text(b))
        b.auto_return = {"plan": 1, "address": ADDR}
        self.assertIn("not signed yet", onchain.auto_return_text(b))
        b.auto_return = {"plan": 1, "address": ADDR, "sigs": ["aa"], "tx": "00"}
        self.assertIn("goes back by itself", onchain.auto_return_text(b))
        b.status = OnchainBond.Status.RETURNABLE
        self.assertIn("not sent", onchain.auto_return_text(b))
        b.status = OnchainBond.Status.RETURNED
        b.spend_txid = TXID2
        with mock.patch("roblosats_admin.onchain._auto_return_txid", return_value=TXID2):
            self.assertIn("sent by itself", onchain.auto_return_text(b))
        with mock.patch("roblosats_admin.onchain._auto_return_txid", return_value=TXID):
            self.assertEqual(onchain.auto_return_text(b), "")  # returned by hand

    def test_auto_return_column_on_order_and_dispute_pages(self):
        o = make_order(self.maker, self.taker, Order.Status.DIS, escrow_type=Order.EscrowTypes.ONCHAIN)
        Order.objects.filter(pk=o.pk).update(is_disputed=True)
        o.refresh_from_db()
        b = bond(o, self.maker, "maker", OnchainBond.Status.LOCKED, txid=TXID, value=3330)
        b.auto_return = {"plan": 1, "address": ADDR, "sigs": ["aa"], "tx": "00"}
        b.save()
        self._escrow(o)
        r = self.client.get(reverse("admin:api_order_dispute", args=[o.pk]))
        self.assertContains(r, 'id="onchain"')
        self.assertContains(r, "goes back by itself")

    # ------------------------------------------------------------ Spamcoin, payouts
    def test_spamcoin_currency_text(self):
        Currency.objects.get_or_create(currency=1000, defaults={"exchange_rate": 1})
        o = make_order(self.maker, self.taker, Order.Status.SUC)
        o.currency_id = 1000
        self.assertEqual(disputes.currency_text(o), "Spamcoin")

    def _swap_order(self, minutes_ago):
        o = make_order(self.maker, self.taker, Order.Status.SUC)
        tx = OnchainPayment.objects.create(
            concept=OnchainPayment.Concepts.PAYBUYER, status=OnchainPayment.Status.QUEUE,
            address=ADDR, num_satoshis=50000, sent_satoshis=49000, receiver=self.taker,
            suggested_mining_fee_rate=1, mining_fee_rate=1, swap_fee_rate=0, balance=None,
        )
        o.is_swap = True
        o.payout_tx = tx
        o.contract_finalization_time = timezone.now() - timedelta(minutes=minutes_ago)
        o.save()
        return o

    def test_queued_onchain_payouts(self):
        old = self._swap_order(60)
        self._swap_order(2)  # just finished: not yet a problem
        ids = [q["id"] for q in payouts.queued_onchain()]
        self.assertEqual(ids, [old.pk])
        r = self.client.get(reverse("admin:api_order_failed_payouts"))
        self.assertContains(r, "On-chain payouts not sent yet")

    def test_payout_chain_status_on_order_page(self):
        o = self._swap_order(60)
        o.payout_tx.txid = TXID2
        o.payout_tx.status = OnchainPayment.Status.MEMPO
        o.payout_tx.save()
        r = self.client.get(reverse("admin:api_order_summary", args=[o.pk]))
        self.assertContains(r, "mempool.kilombino.com/tx/" + TXID2)

    # ------------------------------------------------------------ Telegram
    def test_bond_manual_alert_text(self):
        o = make_order(self.maker, self.taker, Order.Status.SUC)
        cfg = TelegramAlerts.objects.create(pk=1, bot_token=TOKEN, chat_id="-100123", on_bond_manual=True)
        self.assertTrue(alerts.wants(cfg, "bond_manual"))
        text = alerts.build_text(cfg, "bond_manual", o, {"role": "maker", "reason": "coins changed"})
        self.assertIn("maker", text)
        self.assertIn("(coins changed)", text)

    def test_failed_auto_return_queues_alert(self):
        from api.escrow import bonds as bond_service

        o = make_order(self.maker, self.taker, Order.Status.SUC, escrow_type=Order.EscrowTypes.ONCHAIN)
        b = bond(o, self.maker, "maker", OnchainBond.Status.RETURNABLE, txid=TXID, value=3330,
                 coins=[[TXID, 0, 3330]])
        b.auto_return = {"plan": 1, "address": ADDR, "sigs": ["aa"], "tx": "00", "coins": [[TXID2, 0, 1]]}
        b.save()
        with mock.patch("roblosats_admin.alerts.queue") as q:
            self.assertFalse(bond_service._broadcast_auto_return(b))
        q.assert_called_once()
        self.assertEqual(q.call_args[0][0], "bond_manual")
