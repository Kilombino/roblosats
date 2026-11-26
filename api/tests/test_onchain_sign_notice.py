"""0.9.3: the apps sign the onchain steps by themselves, but only while the order is open. A
robot whose signature is still pending after a while is told once to open the order."""

import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase

from api.escrow import flow


class SignNotice(SimpleTestCase):
    def setUp(self):
        self.order = Mock(id=int(uuid.uuid4().int % 10**9))
        self.user = SimpleNamespace(id=7, robot=SimpleNamespace(id=3))
        self.escrow = SimpleNamespace(status=1, funding_signatures={})

    def _run(self, t):
        with patch("api.escrow.flow.pending_signatures", return_value=[(self.user, "rescue_signature")]), \
                patch("time.time", return_value=t), patch("api.tasks.send_notification") as sn:
            flow.notify_signatures(self.order, self.escrow)
        return sn

    def test_told_once_after_the_grace_period(self):
        self.assertFalse(self._run(1_000).delay.called)  # first seen: the app may be signing now
        self.assertFalse(self._run(1_000 + flow.SIGN_NOTICE_AFTER - 1).delay.called)
        sn = self._run(1_000 + flow.SIGN_NOTICE_AFTER)
        sn.delay.assert_called_once_with(order_id=self.order.id, message="onchain_sign:7:rescue_signature")
        self.assertFalse(self._run(1_000 + 10 * flow.SIGN_NOTICE_AFTER).delay.called)

    def test_texts(self):
        from api.notifications import Notifications

        n = Notifications()
        for lang, word in (("es", "Abre la orden"), ("ca", "Obre l'ordre"), (None, "Open order")):
            robot = SimpleNamespace(telegram_lang_code=lang)
            user = SimpleNamespace(id=7, robot=robot)
            order = SimpleNamespace(id=24, maker_id=7, maker=user, taker=None)
            with patch.object(Notifications, "send_message") as sm, patch.object(Notifications, "order_url", return_value="u"):
                n.onchain_sign_needed(order, 7, "deposit_signature")
            self.assertIn(word, sm.call_args[0][2])
            self.assertIn("24", sm.call_args[0][2])
        cache.clear()


class BondConfirmedNotice(SimpleTestCase):
    """0.9.14-beta6 (Kilombino TG 15636): a bond that just got its confirmation and waits for
    its app's signature is told at once, once; the 90 s notice does not repeat it."""

    def setUp(self):
        cache.clear()
        self.order = Mock(id=int(uuid.uuid4().int % 10**9))
        self.user = SimpleNamespace(id=8, robot=SimpleNamespace(id=4))

    def _run(self, t, what="take_bond_signature"):
        with patch("api.escrow.flow.pending_signatures", return_value=[(self.user, what)]), \
                patch("time.time", return_value=t), patch("api.tasks.send_notification") as sn:
            flow.notify_signatures(self.order, None)
        return sn

    def test_told_at_once_and_only_once(self):
        sn = self._run(5_000)
        sn.delay.assert_called_once_with(order_id=self.order.id, message="onchain_sign:8:take_bond_signature:confirmed")
        self.assertFalse(self._run(5_001).delay.called)
        self.assertFalse(self._run(5_000 + 10 * flow.SIGN_NOTICE_AFTER).delay.called)  # no 90 s repeat

    def test_makers_bond_too(self):
        sn = self._run(5_000, what="bond_signature")
        sn.delay.assert_called_once_with(order_id=self.order.id, message="onchain_sign:8:bond_signature:confirmed")

    def test_texts_for_a_robot_still_taking(self):
        from api.notifications import Notifications

        n = Notifications()
        for lang, word in (("es", "otro robot aún puede ganarla"), ("ca", "encara la pot guanyar"),
                           (None, "another robot can still win it")):
            user = SimpleNamespace(id=8, robot=SimpleNamespace(telegram_lang_code=lang))
            order = SimpleNamespace(id=35, maker_id=1, maker=SimpleNamespace(id=1), taker=None)
            with patch.object(Notifications, "send_message") as sm, \
                    patch.object(Notifications, "order_url", return_value="u"), \
                    patch("api.models.TakeOrder.objects.filter") as tf, \
                    patch("django.contrib.auth.models.User.objects.filter") as uf:
                tf.return_value.exists.return_value = True
                uf.return_value.select_related.return_value.first.return_value = user
                n.onchain_sign_needed(order, 8, "take_bond_signature", confirmed=True)
            text = sm.call_args[0][2]
            self.assertIn("✅", text)
            self.assertIn("35", text)
            self.assertIn(word, text)

    def test_task_parses_the_confirmed_flag(self):
        from api import tasks

        with patch("api.models.Order.objects.get", return_value=Mock(id=35)), \
                patch("api.notifications.Notifications.onchain_sign_needed") as osn:
            tasks.send_notification(order_id=35, message="onchain_sign:8:take_bond_signature:confirmed")
            tasks.send_notification(order_id=35, message="onchain_sign:8:rescue_signature")
        self.assertEqual(osn.call_args_list[0].kwargs, {"confirmed": True})
        self.assertEqual(osn.call_args_list[1].kwargs, {"confirmed": False})
