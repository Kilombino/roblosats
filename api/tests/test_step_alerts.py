"""0.9.4: a notice for every order step (api/step_alerts.py). Channels are mocked: these tests
check which robot is told what, in which language, with the order number, the action flag and
no repeats."""

import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from api import step_alerts as sa
from api.models import Order

S = Order.Status


class FakeCache:
    def __init__(self):
        self.d = {}

    def add(self, k, v, timeout=None):
        if k in self.d:
            return False
        self.d[k] = v
        return True

    def get(self, k, default=None):
        return self.d.get(k, default)


def robot(lang=None, tg=False, webhook=False, nostr="ab" * 32):
    return SimpleNamespace(
        id=1, telegram_lang_code=lang, telegram_enabled=tg, telegram_chat_id=5 if tg else None,
        webhook_enabled=webhook, webhook_url="http://x.onion/h", webhook_api_key=None,
        nostr_pubkey=nostr, hash_id="h",
    )


def user(uid, lang=None, **kw):
    return SimpleNamespace(id=uid, robot=robot(lang, **kw), username=f"r{uid}")


def order(type_=Order.Types.SELL, onchain=False, status=S.PUB, taker=True, **kw):
    maker = user(1, kw.pop("maker_lang", None))
    tk = user(2, kw.pop("taker_lang", None)) if taker else None
    o = SimpleNamespace(
        id=24, type=type_, status=status, maker=maker, maker_id=1, taker=tk, taker_id=2 if taker else None,
        escrow_type=Order.EscrowTypes.ONCHAIN if onchain else Order.EscrowTypes.LIGHTNING,
        expires_at=timezone.now() + timedelta(hours=2), expiry_reason=None, last_satoshis=100_000,
        trade_escrow=None, payout=None, payout_tx=None, taker_lightning=False,
        maker_statement=None, taker_statement=None,
    )
    o.__dict__.update(kw)
    return o


class Base(SimpleTestCase):
    def setUp(self):
        self.cache = FakeCache()
        p = patch.object(sa, "cache", self.cache)
        p.start()
        self.addCleanup(p.stop)

    def told(self, o, old, new, taker_id=None):
        calls = []

        def fake_tell(order, u, key, action=False, dedup=None, **values):
            calls.append((u.id if u else None, key, action))
            return True

        with patch.object(sa, "tell", side_effect=fake_tell), patch.object(sa, "_onchain_summary", return_value=None):
            sa.on_status(o, old, new, taker_id)
        return calls


class Texts(SimpleTestCase):
    def test_every_text_in_three_languages_with_the_same_fields(self):
        for table in (sa.TEXTS, sa.WHAT, sa.CONSEQUENCE, sa.PEER):
            for key, row in table.items():
                self.assertEqual(len(row), 3, key)
                fields = [sorted(re.findall(r"{(\w+)}", t)) for t in row]
                self.assertTrue(fields[0] == fields[1] == fields[2], f"{key}: {fields}")

    def test_step_texts_carry_the_order_number(self):
        nested = {k for k in sa.TEXTS if k.startswith(("next_", "how_", "collab_detail_", "reward_now", "reward_later"))}
        for key, row in sa.TEXTS.items():
            if key in nested:
                continue
            for t in row:
                self.assertIn("{n}", t, key)

    def test_language_of_the_robot(self):
        for code, lang in (("es", "es"), ("es-ES", "es"), ("ca", "ca"), ("ca_ES", "ca"), ("en", "en"),
                           (None, "en"), ("de", "en"), ("cat", "en")):
            self.assertEqual(sa.lang_of(SimpleNamespace(telegram_lang_code=code)), lang, code)

    def test_examples(self):
        self.assertEqual(
            sa.text("deadline", "en", n=24, left="30 min", what=("WHAT", "escrow_ln"), cons=("CONSEQUENCE", "bond")),
            "⏰ Order 24: 30 min left to lock the trade escrow, or you lose your bond.",
        )
        self.assertEqual(
            sa.text("deadline", "es", n=24, left="10 min", what=("WHAT", "fiat_sent"), cons=("CONSEQUENCE", "dispute")),
            "⏰ Orden 24: quedan 10 min para enviar el pago y pulsar \"Confirmar enviado\"; si no, se abre una disputa automáticamente.",
        )
        self.assertEqual(
            sa.text("contract_taker", "ca", n=7, next=("next_payout_ln", {"sats": "95.000", "left": "2 h"})),
            "🤝 La teva fiança està bloquejada: l'ordre 7 és teva. Ara: envia la teva factura de cobrament (95.000 sats) abans de 2 h.",
        )
        self.assertEqual(
            sa.text("exp_fault", "es", n=3, what=("WHAT", "escrow_onchain")),
            "⌛ La orden 3 ha caducado: no llegaste a financiar el depósito on-chain a tiempo. Tu fianza se ha retenido como penalización.",
        )
        self.assertEqual(
            sa.text("reward", "en", n=9, sats="1,500", when=("reward_later", {"wait": "1 h"})),
            "🎁 Order 9: you got 1,500 sats of rewards (part of the other trader's bond). "
            "You can withdraw them from your robot page in 1 h.",
        )
        self.assertEqual(
            sa.text("chat_buyer", "en", n=5, how=("how_onchain", {})),
            "💬 Order 5: the deposit is confirmed, the escrow is locked and the chat is open. "
            "Send the payment to the seller as agreed and press \"Confirm sent\".",
        )

    def test_formats(self):
        self.assertEqual(sa.fmt_sats(1234567, "en"), "1,234,567")
        self.assertEqual(sa.fmt_sats(1234567, "es"), "1.234.567")
        self.assertEqual(sa.fmt_left(299), "5 min")
        self.assertEqual(sa.fmt_left(3 * 3600), "3 h")
        self.assertEqual(sa.fmt_left(9000), "2 h 30 min")


class LightningSellMakerIsSeller(Base):
    """Maker sells (seller=1), taker buys (buyer=2), Lightning."""

    def test_published(self):
        self.assertEqual(self.told(order(status=S.PUB), S.WFB, S.PUB), [(1, "published", False)])

    def test_paused_and_resumed_say_nothing(self):
        self.assertEqual(self.told(order(status=S.PAU), S.PUB, S.PAU), [])
        self.assertEqual(self.told(order(status=S.PUB), S.PAU, S.PUB), [])

    def test_contract_tells_both_what_comes_next(self):
        calls = self.told(order(status=S.WF2), S.PUB, S.WF2)
        self.assertEqual(calls, [(1, "contract_maker", True), (2, "contract_taker", True)])

    def test_buyer_ready_tells_the_seller_to_lock(self):
        self.assertEqual(self.told(order(status=S.WFE), S.WF2, S.WFE), [(1, "seller_lock_now", True)])

    def test_escrow_locked_tells_the_buyer_to_send_the_invoice(self):
        self.assertEqual(self.told(order(status=S.WFI), S.WF2, S.WFI), [(2, "buyer_payout_now", True)])

    def test_chat_open(self):
        self.assertEqual(self.told(order(status=S.CHA), S.WFE, S.CHA), [(2, "chat_buyer", True), (1, "chat_seller", False)])

    def test_fiat_sent_and_undone_go_to_the_seller_only(self):
        self.assertEqual(self.told(order(status=S.FSE), S.CHA, S.FSE), [(1, "fiat_sent", True)])
        self.assertEqual(self.told(order(status=S.CHA), S.FSE, S.CHA), [(1, "fiat_sent_undone", False)])

    def test_paying_success_and_failure(self):
        self.assertEqual(self.told(order(status=S.PAY), S.FSE, S.PAY), [(2, "paying_ln", False)])
        self.assertEqual(self.told(order(status=S.SUC), S.PAY, S.SUC), [(2, "success_buyer", False), (1, "success_seller", False)])
        self.assertEqual(self.told(order(status=S.FAI), S.PAY, S.FAI), [(2, "payout_failed", True)])

    def test_dispute(self):
        self.assertEqual(self.told(order(status=S.DIS), S.CHA, S.DIS), [(1, "dispute_opened", True), (2, "dispute_opened", True)])
        self.assertEqual(self.told(order(status=S.WFR), S.DIS, S.WFR), [(1, "dispute_wait", False), (2, "dispute_wait", False)])
        self.assertEqual(self.told(order(status=S.MLD), S.WFR, S.MLD), [(1, "dispute_lost", False), (2, "dispute_won_buyer", False)])
        self.assertEqual(self.told(order(status=S.TLD), S.WFR, S.TLD), [(2, "dispute_lost", False), (1, "dispute_won_seller", False)])

    def test_collaborative_cancel(self):
        self.assertEqual(self.told(order(status=S.CCA), S.CHA, S.CCA), [(1, "collab_done", False), (2, "collab_done", False)])

    def test_maker_cancelled_before_escrow_tells_the_taker(self):
        self.assertEqual(self.told(order(status=S.UCA), S.WFE, S.UCA), [(2, "maker_cancelled", False)])
        self.assertEqual(self.told(order(status=S.UCA, taker=False), S.PUB, S.UCA), [])

    def test_taker_dropped_back_to_the_book(self):
        o = order(status=S.PUB, taker=False)
        with patch.object(sa, "_user", return_value=user(2)):
            calls = self.told(o, S.WFI, S.PUB, taker_id=2)
        self.assertEqual(calls, [(1, "republished", False), (2, "taker_dropped", False)])

    def test_expiry(self):
        self.assertEqual(self.told(order(status=S.EXP, taker=False), S.WFB, S.EXP), [(1, "exp_nobond", False)])
        self.assertEqual(self.told(order(status=S.EXP, taker=False), S.PUB, S.EXP), [(1, "exp_untaken", False)])
        self.assertEqual(self.told(order(status=S.EXP), S.WF2, S.EXP), [(1, "exp_both", False), (2, "exp_both", False)])
        o = order(status=S.EXP, expiry_reason=Order.ExpiryReasons.NESCRO)
        self.assertEqual(self.told(o, S.WFE, S.EXP), [(1, "exp_fault", False), (2, "exp_victim", False)])
        o = order(status=S.EXP, expiry_reason=Order.ExpiryReasons.NINVOI)
        self.assertEqual(self.told(o, S.WFI, S.EXP), [(2, "exp_fault", False), (1, "exp_victim", False)])


class BuyOrderMakerIsBuyer(Base):
    """Maker buys (buyer=1), taker sells (seller=2): the roles swap."""

    def test_roles_swap(self):
        o = lambda st: order(type_=Order.Types.BUY, status=st)  # noqa: E731
        self.assertEqual(self.told(o(S.WFE), S.WF2, S.WFE), [(2, "seller_lock_now", True)])
        self.assertEqual(self.told(o(S.WFI), S.WF2, S.WFI), [(1, "buyer_payout_now", True)])
        self.assertEqual(self.told(o(S.FSE), S.CHA, S.FSE), [(2, "fiat_sent", True)])
        self.assertEqual(self.told(o(S.MLD), S.WFR, S.MLD), [(1, "dispute_lost", False), (2, "dispute_won_seller", False)])


class Onchain(Base):
    def test_funding_step_comes_from_the_onchain_watcher(self):
        self.assertEqual(self.told(order(onchain=True, status=S.WFE), S.WF2, S.WFE), [])

    def test_release_is_signed_by_the_buyer_app(self):
        self.assertEqual(self.told(order(onchain=True, status=S.PAY), S.FSE, S.PAY), [(2, "releasing_onchain", True)])

    def test_contract_asks_for_the_addresses(self):
        o = order(onchain=True, status=S.WF2)
        sent = []
        with patch.object(sa, "tell", side_effect=lambda order, u, key, action=False, dedup=None, **v: sent.append((u.id, key, v))):
            sa.on_status(o, S.PUB, S.WF2)
        nexts = {uid: v["next"][0] for uid, key, v in sent}
        self.assertEqual(nexts, {1: "next_address_seller", 2: "next_address_buyer"})

    def test_success_with_amounts(self):
        o = order(onchain=True, status=S.SUC)
        sent = []
        summaries = {1: {"received_sats": 30_000}, 2: {"received_sats": 99_000}}
        with patch.object(sa, "tell", side_effect=lambda order, u, key, action=False, dedup=None, **v: sent.append((u.id, key, v.get("sats")))), \
                patch.object(sa, "_onchain_summary", side_effect=lambda order, u: summaries[u.id]):
            sa.on_status(o, S.PAY, S.SUC)
        self.assertEqual(sent, [(2, "success_buyer", 99_000), (1, "success_seller_onchain", 30_000)])

    def test_watcher_tells_the_seller_to_fund_once(self):
        from api.models import OnchainEscrow

        o = order(onchain=True, status=S.WFE)
        escrow = SimpleNamespace(id=3, status=OnchainEscrow.Status.KEYS, funding_txid=None, release_txid=None)
        flow = MagicMock()
        flow.waiting_for.side_effect = lambda order, e, role: ("you", "funding") if role == "seller" else ("peer", "funding")
        # (owed, what the screen asks for): #38 said 21,379 while the screen asked 21,945
        flow.recommended_funding.side_effect = lambda order, e, role: (1_000_889, 1_001_455) if role == "seller" else (0, 0)
        flow.service.funding_utxos.return_value = []
        queued = []
        import api.escrow as escrow_pkg

        with patch.object(sa, "queue", side_effect=lambda order, u, key, **kw: queued.append((u.id, key, kw.get("sats")))):
            with patch.object(escrow_pkg, "flow", flow):
                sa.watch_onchain(o, escrow)
                self.cache.d.pop(f"step-alert-watch:{o.id}")
                sa.watch_onchain(o, escrow)
        self.assertEqual(queued[0], (1, "fund_now", 1_001_455))


class DedupAndChannels(Base):
    def _notifications(self):
        from api.notifications import Notifications

        return Notifications

    def test_one_notice_per_event(self):
        N = self._notifications()
        o = order(maker_lang="es")
        with patch.object(N, "send_message") as sm, patch.object(N, "order_url", return_value="http://k/order/kilobot/24"):
            self.assertTrue(sa.tell(o, o.maker, "fiat_sent", action=True, dedup="x"))
            self.assertFalse(sa.tell(o, o.maker, "fiat_sent", action=True, dedup="x"))
        sm.assert_called_once()
        args, kwargs = sm.call_args
        self.assertIn("Orden 24", args[2])
        self.assertEqual(args[3], "http://k/order/kilobot/24")
        self.assertTrue(kwargs["action"])

    def test_channels_respect_the_robot_toggles(self):
        N = self._notifications()
        n = N()
        o = order()
        with patch("api.notifications.Notification") as model, \
                patch("api.notifications.nostr_send_notification_event") as nostr, \
                patch.object(N, "send_telegram_message") as tg, patch.object(N, "send_webhook_message") as wh:
            n.send_message(o, robot(), "t", "d", action=True)
            nostr.delay.assert_called_once_with(robot_id=1, order_id=24, text="t", action=True)
            tg.assert_not_called()
            wh.assert_not_called()
            n.send_message(o, robot(tg=True, webhook=True, nostr=None), "x" * 300, "d")
            tg.assert_called_once()
            wh.assert_called_once()
            self.assertEqual(nostr.delay.call_count, 1)
            self.assertEqual(len(model.objects.create.call_args.kwargs["title"]), 240)

    def test_telegram_text_is_url_encoded(self):
        N = self._notifications()
        n = N()
        with patch.object(n, "session") as session, patch("api.notifications.config", return_value="T"):
            n.send_telegram_message(5, "a & b #1", "http://x/order/k/1")
        url = session.get.call_args.args[0]
        self.assertNotIn("text=", url)
        self.assertEqual(session.get.call_args.kwargs["params"]["text"], "a & b #1 http://x/order/k/1")


class Deadlines(SimpleTestCase):
    def test_marks(self):
        three_h, two_min = 3 * 3600, 120
        self.assertIsNone(sa.deadline_mark(31 * 60, three_h))
        self.assertEqual(sa.deadline_mark(30 * 60, three_h), 30)
        self.assertEqual(sa.deadline_mark(11 * 60, three_h), 30)
        self.assertEqual(sa.deadline_mark(10 * 60, three_h), 10)
        self.assertEqual(sa.deadline_mark(30, three_h), 10)
        self.assertIsNone(sa.deadline_mark(0, three_h))
        self.assertIsNone(sa.deadline_mark(60, two_min))  # a 2-minute Lightning bond: no reminders
        self.assertIsNone(sa.deadline_mark(25 * 60, 30 * 60))  # a 30-minute step: only the 10-minute one
        self.assertEqual(sa.deadline_mark(9 * 60, 30 * 60), 10)

    def test_who_must_act(self):
        def targets(st, **kw):
            return [(u.id if u else None, w, c) for u, w, c in sa.deadline_targets(order(status=st, **kw))]

        self.assertEqual(targets(S.WFB, taker=False), [(1, "bond_maker", "nothing")])
        self.assertEqual(targets(S.WF2), [(1, "escrow_ln", "bond"), (2, "payout_ln", "bond")])
        self.assertEqual(targets(S.WFE), [(1, "escrow_ln", "bond")])
        self.assertEqual(targets(S.WFI), [(2, "payout_ln", "bond")])
        self.assertEqual(targets(S.CHA), [(2, "fiat_sent", "dispute")])
        self.assertEqual(targets(S.FSE), [(1, "fiat_received", "dispute")])
        self.assertEqual(targets(S.DIS, maker_statement="x" * 100), [(2, "statement", "statement")])

    def test_onchain_targets_follow_the_escrow(self):
        o = order(onchain=True, status=S.WFE)
        flow = MagicMock()
        flow.escrow_of.return_value = SimpleNamespace()
        flow.waiting_for.side_effect = lambda order, e, role: {"seller": ("you", "funding"), "buyer": ("peer", "funding")}[role]
        import api.escrow as escrow_pkg

        with patch.object(escrow_pkg, "flow", flow):
            out = [(u.id, w) for u, w, c in sa.deadline_targets(o)]
        self.assertEqual(out, [(1, "escrow_onchain")])


class Wiring(Base):
    def test_status_change_is_sent_a_few_seconds_later(self):
        o = order(status=S.FSE)
        with patch("django.db.transaction.on_commit", side_effect=lambda f: f()), \
                patch("api.tasks.send_notification") as sn:
            sa.on_status_changed(Order, o, S.CHA, S.FSE)
        sn.apply_async.assert_called_once_with(kwargs=dict(order_id=24, message="step:9:10:2"), countdown=3)

    def test_an_onchain_order_that_ends_checks_for_sats_to_take_back(self):
        o = order(onchain=True, status=S.SUC)
        with patch("django.db.transaction.on_commit", side_effect=lambda f: f()), \
                patch("api.tasks.send_notification") as sn:
            sa.on_status_changed(Order, o, S.PAY, S.SUC)
        messages = [c.kwargs["kwargs"]["message"] for c in sn.apply_async.call_args_list]
        self.assertEqual(messages, ["step:13:14:2", "onchain_end"])

    def test_legacy_messages_do_not_repeat_the_step(self):
        from api import tasks

        with patch("api.models.Order.objects") as objects, patch("api.notifications.Notifications") as N, \
                patch.object(sa, "on_status") as on_status:
            objects.get.return_value = order()
            for legacy in ("order_published", "fiat_exchange_starts", "trade_successful", "lightning_failed"):
                tasks.send_notification(order_id=24, message=legacy)
            self.assertEqual(N.return_value.method_calls, [])
            tasks.send_notification(order_id=24, message="step:9:10:2")
            on_status.assert_called_once_with(objects.get.return_value, 9, 10, 2)

    def test_queue_claims_before_queueing(self):
        o = order()
        with patch("api.tasks.send_step_alert") as task:
            self.assertTrue(sa.queue(o, o.taker, "collab_asked", action=True, dedup="c", peer=("PEER", "seller")))
            self.assertFalse(sa.queue(o, o.taker, "collab_asked", action=True, dedup="c", peer=("PEER", "seller")))
        task.apply_async.assert_called_once()

    def test_peer_events(self):
        o = order(status=S.CHA)
        with patch.object(sa, "queue") as q:
            sa.on_collab_asked(o, o.maker)
            self.assertEqual(q.call_args.args[1].id, 2)
            self.assertEqual(q.call_args.args[2], "collab_asked")
            self.assertEqual(q.call_args.kwargs["peer"], ("PEER", "seller"))
            o2 = order(status=S.DIS, maker_statement="x" * 100)
            sa.on_statement(o2, o2.maker)
            self.assertEqual((q.call_args.args[1].id, q.call_args.args[2]), (2, "statement_peer"))
            q.reset_mock()
            o3 = order(status=S.DIS, maker_statement="x" * 100, taker_statement="y" * 100)
            sa.on_statement(o3, o3.maker)
            q.assert_not_called()

    def test_onchain_take_tells_taker_and_maker(self):
        o = order(onchain=True, taker=False)
        take = SimpleNamespace(id=8, lightning=False, taker=user(2), expires_at=timezone.now() + timedelta(hours=3))
        with patch.object(sa, "queue") as q:
            sa.on_take_started(o, take)
            self.assertEqual([(c.args[1].id, c.args[2]) for c in q.call_args_list], [(2, "take_started"), (1, "being_taken")])
            q.reset_mock()
            sa.on_take_started(o, SimpleNamespace(id=9, lightning=True, taker=user(2), expires_at=take.expires_at))
            q.assert_not_called()


class AmountsMatchTheScreen(Base):
    """0.9.14-beta7 (#38): the alerts give the amount the order screen asks for."""

    def test_ln_escrow_before_its_invoice_is_the_invoice_amount(self):
        o = order()
        o.trade_escrow = None
        with patch("api.logics.Logics.escrow_amount", return_value=(True, {"escrow_amount": 1_000_875})) as ea:
            self.assertEqual(sa._escrow_sats(o), 1_000_875)
        self.assertIn(ea.call_args.args[1], (o.maker, o.taker))

    def test_fee_now_text_counts_the_mining_share(self):
        for lang in ("en", "es", "ca"):
            t = sa.text("fee_now", lang, n=38, sats="431", left="3 h")
            self.assertIn("431", t)
