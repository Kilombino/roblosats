"""0.9.7 watcher alerts (watch.py): dedupe across runs, no flood on the first run, events from
order and LND wallet state, and the texts in English, Spanish and Catalan."""

from unittest import mock

from django.test import TestCase
from django.utils import translation

from api.models import OnchainBond, OnchainEscrow, Order
from roblosats_admin import alerts, watch
from roblosats_admin.models import AlertMark, TelegramAlerts
from roblosats_admin.tests.test_disputes import make_user
from roblosats_admin.tests.test_onchain_admin import TXID, TXID2, bond
from roblosats_admin.tests.test_orders_alerts import TOKEN, make_order

TX3 = "ef" * 32


def snap(**kw):
    s = {"tip": 1000, "txs": [], "open": [], "opening": [], "closing": [], "closed": []}
    s.update(kw)
    return s


class DedupeTest(TestCase):
    def test_fresh_returns_only_new_keys_once(self):
        self.assertEqual(watch.fresh(["a", "b", "a"]), ["a", "b"])
        self.assertEqual(watch.fresh(["b", "c"]), ["c"])
        self.assertEqual(watch.fresh([]), [])
        self.assertEqual(AlertMark.objects.count(), 3)

    def test_first_run_only_remembers(self):
        sent = []
        events = [("k1", "x", {}), ("k2", "y", {})]
        self.assertEqual(watch.dispatch("wallet", events, lambda k, d: sent.append(k)), 0)
        self.assertEqual(sent, [])
        self.assertTrue(watch.started("wallet"))
        # same events again: nothing; one new event: told once
        events.append(("k3", "z", {}))
        self.assertEqual(watch.dispatch("wallet", events, lambda k, d: sent.append(k)), 1)
        self.assertEqual(watch.dispatch("wallet", events, lambda k, d: sent.append(k)), 0)
        self.assertEqual(sent, ["z"])

    def test_sources_start_separately(self):
        watch.dispatch("onchain", [("a", "x", {})], lambda k, d: None)
        self.assertTrue(watch.started("onchain"))
        self.assertFalse(watch.started("wallet"))


class WalletEventsTest(TestCase):
    def test_mempool_then_confirmed(self):
        t = {"txid": TXID, "amount": 50_000, "fee": 0, "confs": 0, "label": ""}
        kinds = [k for _, k, _ in watch.wallet_events(snap(txs=[t]))]
        self.assertEqual(kinds, ["tx_in_mempool"])
        t["confs"] = 1
        ev = watch.wallet_events(snap(txs=[t]))
        self.assertEqual([k for _, k, _ in ev], [None, "tx_in_confirmed"])
        # the "seen" key is the same in both: a confirmed tx is never told as new in the mempool
        self.assertEqual(ev[0][0], f"w:tx-seen:{TXID}")

    def test_outgoing_notable_fee_and_channels(self):
        t = {"txid": TXID2, "amount": -100_000, "fee": 5_000, "confs": 3, "label": "open channel"}
        s = snap(
            txs=[t],
            opening=[{"point": TX3 + ":0", "peer": "02" + "aa" * 32, "capacity": 1_000_000}],
            closing=[{"point": TX3 + ":1", "peer": "03" + "bb" * 32, "capacity": 500_000, "force": True, "txid": TXID}],
            closed=[{"point": TX3 + ":2", "peer": "03" + "cc" * 32, "capacity": 2, "type": "remote_force",
                     "settled": 1, "txid": TXID}],
        )
        kinds = [k for _, k, _ in watch.wallet_events(s, fee_floor=2000)]
        self.assertEqual(kinds, [None, "tx_out_confirmed", "fee_notable", "chan_opening", "chan_closing", "chan_closed"])
        self.assertNotIn("fee_notable", [k for _, k, _ in watch.wallet_events(s, fee_floor=10_000)])


class OrderEventsTest(TestCase):
    def test_full_life_of_an_onchain_order(self):
        state = {
            "order": 7,
            "seen": [{"what": "bond", "role": "taker", "txid": TXID, "sats": 3330, "fee_rate": 1.2},
                     {"what": "funding", "role": "seller", "txid": TXID2, "sats": 70000}],
            "bonds": [{"id": 1, "role": "maker", "status": 6, "txid": TXID, "sats": 3330,
                       "auto_return_txid": TX3, "auto_return_status": "confirmed"}],
            "escrow": {"id": 9, "status": 5, "sats": 70000, "funding_txid": TXID2, "confirmed_height": 950,
                       "release_txid": TX3, "release_status": "confirmed", "release_kind": "x", "leaf": True,
                       "rescued": False, "signed": True},
            "payout": {"txid": TXID, "sats": 50000, "status": "mempool"},
        }
        kinds = [k for _, k, _ in watch.order_events(state)]
        self.assertEqual(kinds, [
            "bond_mempool", "deposit_mempool", "bond_confirmed", "return_sent", "return_confirmed",
            "deposit_signed", "deposit_confirmed", "release_sent", "release_confirmed", "payout_sent",
        ])

    def test_rescue(self):
        state = {"order": 8, "seen": [], "bonds": [], "payout": None,
                 "escrow": {"id": 3, "status": 7, "sats": 1, "funding_txid": TXID, "confirmed_height": 1,
                            "release_txid": TXID2, "release_status": None, "release_kind": "", "leaf": False,
                            "rescued": True, "signed": True}}
        self.assertIn("rescue_sent", [k for _, k, _ in watch.order_events(state)])


class TextsTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        self.order = make_order(self.maker, self.taker, Order.Status.CHA, escrow_type=Order.EscrowTypes.ONCHAIN)
        self.cfg = TelegramAlerts.objects.create(pk=1, bot_token=TOKEN, chat_id="-100123")

    def text(self, lang, event, order, extra):
        with translation.override(alerts.alert_language(lang)):
            return alerts.build_text(self.cfg, event, order, extra)

    def test_onchain_texts(self):
        extra = {"kind": "bond_confirmed", "role": "taker", "sats": 3330, "txid": TXID}
        en = self.text("en", "onchain", self.order, extra)
        self.assertIn(f"Order {self.order.pk}: the taker's on-chain bond is confirmed (3", en)
        self.assertIn("mempool.kilombino.com/tx/" + TXID, en)
        self.assertIn("fianza on-chain", self.text("es", "onchain", self.order, extra))
        self.assertIn("fiança on-chain del prenedor", self.text("ca", "onchain", self.order, extra))
        leaf = self.text("en", "onchain", self.order, {"kind": "release_sent", "leaf": True, "txid": TXID})
        self.assertIn("seller's leaf, one signature", leaf)

    def test_wallet_texts(self):
        extra = {"kind": "tx_out_confirmed", "amount": -100_000, "fee": 1500, "confs": 2, "label": "sweep",
                 "txid": TXID}
        en = self.text("en", "wallet", None, extra)
        self.assertIn("100", en)
        self.assertIn("1", en)
        self.assertIn("«sweep»", en)
        ca = self.text("ca", "wallet", None, extra)
        self.assertIn("El teu moneder", ca)
        closed = self.text("es", "wallet", None, {"kind": "chan_closed", "peer": "02" + "ab" * 32, "capacity": 1,
                                                  "settled": 1, "type": "local_force"})
        self.assertIn("cierre forzado por tu nodo", closed)

    def test_catalan_is_an_alert_language(self):
        self.assertEqual(alerts.alert_language("ca"), "ca")
        self.assertEqual(alerts.alert_language("es"), "es")
        self.assertIn("COMPRA", self.text("ca", "taken", self.order, {}) + " COMPRA")

    def test_group_toggles(self):
        self.assertFalse(alerts.wants(self.cfg, "onchain"))
        self.cfg.on_onchain_events = True
        self.cfg.save()
        self.assertTrue(alerts.wants(self.cfg, "onchain"))
        self.assertFalse(alerts.wants(self.cfg, "wallet"))


class WatchRunTest(TestCase):
    def setUp(self):
        self.maker = make_user("MakerRobot")
        self.taker = make_user("TakerRobot")
        p = mock.patch("api.escrow.flow.mempool_seen", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("api.order_summary.tx_status", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_no_flood_then_one_alert_per_new_event(self):
        o = make_order(self.maker, self.taker, Order.Status.CHA, escrow_type=Order.EscrowTypes.ONCHAIN)
        bond(o, self.maker, "maker", OnchainBond.Status.IN_DEPOSIT, txid=TXID, value=3330)
        escrow = OnchainEscrow.objects.create(order=o, status=OnchainEscrow.Status.LOCKED, escrow_sats=70000,
                                              funding_txid=TXID2, confirmed_height=900, rescue_csv=2016)
        old_tx = {"txid": TX3, "amount": 1000, "fee": 0, "confs": 6, "label": ""}
        with mock.patch("roblosats_admin.watch.lnd_snapshot", return_value=snap(txs=[old_tx])), \
                mock.patch("roblosats_admin.alerts.queue") as q:
            self.assertEqual(watch.watch(), {"onchain": 0, "wallet": 0})  # first run: remember only
            self.assertEqual(watch.watch(), {"onchain": 0, "wallet": 0})
            q.assert_not_called()
        # the release goes out and a payment comes in
        escrow.release_txid = TX3
        escrow.release_kind = OnchainEscrow.ReleaseKind.COOPERATIVE
        escrow.status = OnchainEscrow.Status.RELEASED
        escrow.save()
        new_tx = {"txid": "12" * 32, "amount": 7000, "fee": 0, "confs": 0, "label": ""}
        with mock.patch("roblosats_admin.watch.lnd_snapshot", return_value=snap(txs=[old_tx, new_tx])), \
                mock.patch("roblosats_admin.alerts.queue") as q:
            self.assertEqual(watch.watch(), {"onchain": 1, "wallet": 1})
            calls = [(c.args[0], c.args[2]["kind"]) for c in q.call_args_list]
            self.assertEqual(sorted(calls), [("onchain", "release_sent"), ("wallet", "tx_in_mempool")])
            self.assertEqual(watch.watch(), {"onchain": 0, "wallet": 0})

    def test_lnd_down_does_not_break_the_order_alerts(self):
        with mock.patch("roblosats_admin.watch.lnd_snapshot", return_value=None):
            self.assertEqual(watch.watch(), {"onchain": 0, "wallet": 0})
        self.assertFalse(watch.started("wallet"))
