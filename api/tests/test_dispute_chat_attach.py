"""Roblosats (26 nov 2026, Kilombino TG 15581/15584): a party attaches its decrypted chat to a
dispute after its statement, once, while the dispute is open; the admin page shows it with
when it was attached and the coordinator is alerted."""

import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api.logics import Logics
from api.models import Currency, LNPayment, Order

STATEMENT = "telegram: @someone \n\n " + "I paid the fiat and the seller does not answer. " * 4
CHAT = json.dumps([
    {"index": 1, "plainTextMessage": "hello", "validSignature": True, "userNick": "a", "time": "2026-11-26T08:00:00+00:00"},
    {"index": 2, "plainTextMessage": "paid", "validSignature": True, "userNick": "b", "time": "2026-11-26T08:01:00+00:00"},
])


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


# 0.9.17: these robots have no PGP keys, so they act as an app too old to sign
# (transition mode); the signature gate itself is tested in test_signed_actions.
@patch("api.utils.REQUIRE_SIGNED_ACTIONS", False)
class AttachChat(TestCase):
    def setUp(self):
        self.patches = [patch("api.logics.send_notification"), patch("api.models.order.send_notification"),
                        patch("roblosats_admin.alerts.queue")]
        self.mocks = [p.start() for p in self.patches]
        self.alert = self.mocks[2]
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        now = timezone.now()
        self.maker, self.taker = _user(), _user()
        self.order = Order.objects.create(
            currency=currency, type=Order.Types.SELL, status=Order.Status.DIS, created_at=now,
            expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100, satoshis=1_000_000,
            t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=self.maker, taker=self.taker, bond_size=3,
            is_explicit=True, is_disputed=True, maker_statement=STATEMENT,
        )
        for side, user, concept in (("maker_bond", self.maker, LNPayment.Concepts.MAKEBOND),
                                    ("taker_bond", self.taker, LNPayment.Concepts.TAKEBOND),
                                    ("trade_escrow", self.maker, LNPayment.Concepts.TRESCROW)):
            setattr(self.order, side, LNPayment.objects.create(
                concept=concept, type=LNPayment.Types.HOLD, sender=user, status=LNPayment.Status.SETLED,
                num_satoshis=30_000, payment_hash=uuid.uuid4().hex, preimage=uuid.uuid4().hex,
                invoice=f"lnbcrt{uuid.uuid4().hex}", created_at=now, expires_at=now + timezone.timedelta(hours=3)))
        self.order.save()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _call(self, user, body):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from api.views import OrderView

        req = APIRequestFactory().post(f"/api/order/?order_id={self.order.id}", body, format="json")
        force_authenticate(req, user=user)
        return OrderView.as_view({"post": "take_update_confirm_dispute_cancel"})(req)

    def test_attach_once_after_the_statement(self):
        ok, err = Logics.attach_dispute_chat(self.order, self.maker, CHAT)
        self.assertTrue(ok, err)
        self.order.refresh_from_db()
        self.assertEqual(len(json.loads(self.order.maker_chat_log)["messages"]), 2)
        self.assertIsNotNone(self.order.maker_chat_log_at)
        self.assertEqual(self.order.maker_statement, STATEMENT)  # the statement is kept as it was
        self.alert.assert_called_once()
        self.assertEqual(self.alert.call_args.args[0], "dispute_evidence")
        ok, err = Logics.attach_dispute_chat(self.order, self.maker, CHAT)
        self.assertFalse(ok)
        self.assertIn("already attached", err["bad_request"])

    def test_not_before_the_statement(self):
        ok, err = Logics.attach_dispute_chat(self.order, self.taker, CHAT)
        self.assertFalse(ok)
        self.assertIn("statement first", err["bad_request"])

    def test_not_when_the_statement_already_carried_the_chat(self):
        self.order.taker_statement = json.dumps({"statement": STATEMENT, "messages": json.loads(CHAT)})
        self.order.save()
        self.assertTrue(Logics.chat_attached(self.order, "taker"))
        ok, err = Logics.attach_dispute_chat(self.order, self.taker, CHAT)
        self.assertFalse(ok)

    def test_not_once_resolved(self):
        self.order.status = Order.Status.MLD
        self.order.save()
        ok, err = Logics.attach_dispute_chat(self.order, self.maker, CHAT)
        self.assertFalse(ok)
        self.assertIn("no longer open", err["bad_request"])

    def test_also_while_waiting_for_the_decision(self):
        self.order.status = Order.Status.WFR
        self.order.save()
        ok, err = Logics.attach_dispute_chat(self.order, self.maker, CHAT)
        self.assertTrue(ok, err)

    def test_not_a_chat(self):
        ok, err = Logics.attach_dispute_chat(self.order, self.maker, "hello")
        self.assertFalse(ok)

    def test_only_the_parties_through_the_view(self):
        r = self._call(_user(), {"action": "attach_chat", "chat_log": CHAT})
        self.assertEqual(r.status_code, 403, r.data)
        r = self._call(self.maker, {"action": "attach_chat", "chat_log": CHAT})
        self.assertEqual(r.status_code, 200, r.data)
        self.order.refresh_from_db()
        self.assertTrue(self.order.maker_chat_log)
        self.assertFalse(self.order.taker_chat_log)

    def test_statement_with_a_long_chat_apart(self):
        r = self._call(self.taker, {"action": "submit_statement", "statement": STATEMENT, "chat_log": CHAT})
        self.assertEqual(r.status_code, 200, r.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.taker_statement, STATEMENT.strip())
        self.assertTrue(self.order.taker_chat_log)
        self.assertEqual(self.order.status, Order.Status.WFR)

    def test_admin_page_shows_the_late_chat_with_its_time(self):
        from roblosats_admin.disputes import parse_statement, with_late_chat

        Logics.attach_dispute_chat(self.order, self.maker, CHAT)
        self.order.refresh_from_db()
        st = with_late_chat(parse_statement(self.order.maker_statement), self.order.maker_chat_log,
                            self.order.maker_chat_log_at)
        self.assertEqual(len(st["messages"]), 2)
        self.assertTrue(st["chat_attached_at"])
        self.assertTrue(st["text"].startswith("telegram"))
        self.assertIsNone(with_late_chat(None, None, None))
