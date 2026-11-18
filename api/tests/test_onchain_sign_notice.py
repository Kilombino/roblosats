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
