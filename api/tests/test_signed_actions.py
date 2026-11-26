"""
Roblosats 0.9.17 — signed actions (GHSA-22q8-9ghc-jg3h).

The bearer token (sent to every coordinator of the federation) only reads. Every write carries a
PGP signature by the robot's key bound to this coordinator: order actions sign
'{action}:{order_id}:{shortAlias}', writes outside an order '{action}:{shortAlias}:{unix_ts}',
the review token 'review:{pubkey}'.

Transition mode (REQUIRE_SIGNED_ACTIONS off): unsigned writes keep working for robots that never
signed (old apps), with one in-app notice; a robot that signed once is refused any unsigned write
from then on; a signature that is present is always verified.

These go through the real views with the test robots' keys; Lightning and the onchain escrow
are mocked (only the signature gate is under test here).
"""

import time
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

import api.utils as api_utils
from api.models import Currency, LNPayment, Notification, Order, Robot
from api.utils import (
    get_federation_short_alias,
    validate_action_signature,
    validate_signed_action_timestamp,
)
from tests.utils.pgp import sign_action


def read_file(path):
    with open(path, "r") as f:
        return f.read()


def auth(index):
    return {
        "HTTP_AUTHORIZATION": (
            f"Token {read_file(f'tests/robots/{index}/b91_token')}"
            f" | Public {read_file(f'tests/robots/{index}/pub_key')}"
            f" | Private {read_file(f'tests/robots/{index}/enc_priv_key')}"
            f" | Nostr {read_file(f'tests/robots/{index}/nostr_pubkey')}"
        )
    }


def timed(action, index, alias=None, ts=None):
    alias = get_federation_short_alias() if alias is None else alias
    ts = int(time.time()) if ts is None else ts
    return sign_action(f"{action}:{alias}:{ts}", index)


def order_sig(action, order_id, index, alias=None):
    alias = get_federation_short_alias() if alias is None else alias
    return sign_action(f"{action}:{order_id}:{alias}", index)


# ---------------------------------------------------------------------------------------------
# Unit tests of the validators (ported from upstream RoboSats PR #2955), mock users
# ---------------------------------------------------------------------------------------------


class ValidatorUnitBase(TestCase):
    def setUp(self):
        robot = MagicMock()
        robot.public_key = "pub_key_block"
        robot.signs_actions_since = None
        for target, kwargs in (
            ("api.utils.Robot.objects.get", {"return_value": robot}),
            ("api.utils._mark_signed", {"return_value": None}),
            ("api.utils.get_federation_short_alias", {"return_value": "kilobot"}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.robot = robot
        self.user = MagicMock()


class ValidateActionSignatureUnitTest(ValidatorUnitBase):
    def test_transition_allows_unsigned_for_a_robot_that_never_signed(self):
        with patch("api.utils.REQUIRE_SIGNED_ACTIONS", False):
            self.assertEqual(
                validate_action_signature(self.user, "confirm:1:kilobot", None), (True, None)
            )

    def test_transition_refuses_unsigned_once_the_robot_signed(self):
        self.robot.signs_actions_since = timezone.now()
        with patch("api.utils.REQUIRE_SIGNED_ACTIONS", False):
            valid, error = validate_action_signature(self.user, "confirm:1:kilobot", None)
        self.assertFalse(valid)
        self.assertEqual(error["error_code"], 1059)

    def test_enforced_refuses_unsigned(self):
        with patch("api.utils.REQUIRE_SIGNED_ACTIONS", True):
            valid, error = validate_action_signature(self.user, "confirm:1:kilobot", "")
        self.assertFalse(valid)
        self.assertEqual(error["error_code"], 1059)

    def test_bad_signature_refused_even_in_transition(self):
        for flag in (False, True):
            with (
                patch("api.utils.REQUIRE_SIGNED_ACTIONS", flag),
                patch("api.utils.verify_signed_message", return_value=(False, None)),
            ):
                valid, error = validate_action_signature(self.user, "confirm:1:kilobot", "sig")
            self.assertFalse(valid)
            self.assertEqual(error["error_code"], 1048)

    def test_content_mismatch_refused(self):
        for content in ("cancel:1:kilobot", "confirm:2:kilobot", "confirm:1:rosegarden"):
            with patch("api.utils.verify_signed_message", return_value=(True, content)):
                valid, error = validate_action_signature(self.user, "confirm:1:kilobot", "sig")
            self.assertFalse(valid)
            self.assertEqual(error["error_code"], 1048)

    def test_valid_signature_accepted_and_marks(self):
        with (
            patch("api.utils.verify_signed_message", return_value=(True, "confirm:1:kilobot")),
            patch("api.utils._mark_signed") as mark,
        ):
            self.assertEqual(
                validate_action_signature(self.user, "confirm:1:kilobot", "sig"), (True, None)
            )
        mark.assert_called_once_with(self.user)

    def test_shadow_mode_logs_once_per_user(self):
        api_utils.SHADOW_MODE_WARNED.clear()
        self.addCleanup(api_utils.SHADOW_MODE_WARNED.clear)
        with (
            patch("api.utils.REQUIRE_SIGNED_ACTIONS", False),
            self.assertLogs("api.utils", level="WARNING") as logged,
        ):
            validate_action_signature(self.user, "confirm:1:kilobot", None)
            validate_action_signature(self.user, "confirm:1:kilobot", None)
        self.assertEqual(len(logged.output), 1)


class ValidateTimedSignatureUnitTest(ValidatorUnitBase):
    def _check(self, content):
        with (
            patch("api.utils.REQUIRE_SIGNED_ACTIONS", True),
            patch("api.utils.verify_signed_message", return_value=(True, content)),
        ):
            return validate_signed_action_timestamp(self.user, "make", "sig")

    def test_fresh_accepted(self):
        self.assertEqual(self._check(f"make:kilobot:{int(time.time())}"), (True, None))

    def test_other_coordinator_1061(self):
        valid, error = self._check(f"make:rosegarden:{int(time.time())}")
        self.assertFalse(valid)
        self.assertEqual(error["error_code"], 1061)

    def test_stale_1060(self):
        valid, error = self._check(f"make:kilobot:{int(time.time()) - 301}")
        self.assertEqual(error["error_code"], 1060)

    def test_malformed_1048(self):
        for content in ("stealth:kilobot:1", "make:kilobot:abc", "make:kilobot", "make"):
            self.assertEqual(self._check(content)[1]["error_code"], 1048, content)


# ---------------------------------------------------------------------------------------------
# Through the views, with real keys
# ---------------------------------------------------------------------------------------------


class ViewsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        Currency.objects.get_or_create(currency=1, defaults={"exchange_rate": Decimal("60000")})

    def setUp(self):
        self.patches = [
            patch("api.logics.send_notification"),
            patch("api.models.order.send_notification"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        for index in (1, 2, 3):
            self.client.get(reverse("robot"), **auth(index))
        api_utils.SHADOW_MODE_WARNED.clear()

    def user(self, index):
        return User.objects.get(username=read_file(f"tests/robots/{index}/nickname").strip())

    def robot(self, index):
        return Robot.objects.get(user=self.user(index))

    def order(self, maker=1, taker=None, status=Order.Status.PUB):
        now = timezone.now()
        order = Order.objects.create(
            currency=Currency.objects.get(currency=1), type=Order.Types.BUY, status=status,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False,
            amount=100, satoshis=200_000, t0_satoshis=200_000, last_satoshis=200_000,
            maker=self.user(maker), taker=self.user(taker) if taker else None, bond_size=3,
            is_explicit=True, payment_method="SEPA",
        )
        order.maker_bond = LNPayment.objects.create(
            concept=LNPayment.Concepts.MAKEBOND, type=LNPayment.Types.HOLD,
            sender=self.user(maker), status=LNPayment.Status.LOCKED, num_satoshis=6_000,
            payment_hash=uuid.uuid4().hex, preimage=uuid.uuid4().hex,
            invoice=f"lnbcrt{uuid.uuid4().hex}", created_at=now,
            expires_at=now + timezone.timedelta(hours=3),
        )
        order.save()
        return order

    def act(self, order_id, action, index, signature=None, **extra):
        body = {"action": action, **extra}
        if signature is not None:
            body["signature"] = signature
        return self.client.post(
            reverse("order") + f"?order_id={order_id}", body, content_type="application/json", **auth(index)
        )

    def stealth(self, index, signature=None):
        body = {"wantsStealth": True}
        if signature is not None:
            body["signature"] = signature
        return self.client.post(
            reverse("stealth"), body, content_type="application/json", **auth(index)
        )


@patch("api.utils.REQUIRE_SIGNED_ACTIONS", False)
class TransitionViewsTest(ViewsBase):
    def test_old_app_keeps_working_and_is_told_once(self):
        order = self.order()
        self.assertEqual(self.act(order.id, "pause", 1).status_code, 200)
        self.assertEqual(Order.objects.get(id=order.id).status, Order.Status.PAU)
        self.assertEqual(self.act(order.id, "pause", 1).status_code, 200)
        self.assertEqual(Order.objects.get(id=order.id).status, Order.Status.PUB)
        notices = Notification.objects.filter(robot=self.robot(1), title__contains="Roblosats")
        self.assertEqual(notices.count(), 1)
        self.assertIsNone(self.robot(1).signs_actions_since)
        self.assertEqual(self.stealth(1).status_code, 200)

    def test_signed_once_then_unsigned_refused_everywhere(self):
        order = self.order()
        self.assertEqual(self.stealth(1, timed("stealth", 1)).status_code, 200)
        self.assertIsNotNone(self.robot(1).signs_actions_since)
        response = self.act(order.id, "pause", 1)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], 1059)
        self.assertEqual(Order.objects.get(id=order.id).status, Order.Status.PUB)
        self.assertEqual(self.stealth(1).json()["error_code"], 1059)
        # no "update your app" notice for a robot whose app signs
        self.assertFalse(Notification.objects.filter(robot=self.robot(1)).exists())
        # signed still works; robot 2 (never signed) is not affected
        self.assertEqual(self.act(order.id, "pause", 1, order_sig("pause", order.id, 1)).status_code, 200)
        self.assertEqual(self.stealth(2).status_code, 200)

    def test_forged_and_replayed_signatures(self):
        order = self.order(maker=1)
        other = self.order(maker=2)
        cases = [
            ("garbage", "not a signature"),
            ("other robot's key", order_sig("pause", order.id, 3)),
            ("other order", order_sig("pause", other.id, 1)),
            ("other action", order_sig("cancel", order.id, 1)),
            ("other coordinator", order_sig("pause", order.id, 1, alias="rosegarden")),
            ("timed form", timed("pause", 1)),
        ]
        for name, sig in cases:
            response = self.act(order.id, "pause", 1, sig)
            self.assertEqual(response.status_code, 400, name)
            self.assertEqual(response.json()["error_code"], 1048, name)
        self.assertEqual(Order.objects.get(id=order.id).status, Order.Status.PUB)
        self.assertIsNone(self.robot(1).signs_actions_since)

    def test_timed_signatures(self):
        self.assertEqual(self.stealth(1, timed("stealth", 1, alias="rosegarden")).json()["error_code"], 1061)
        self.assertEqual(
            self.stealth(1, timed("stealth", 1, ts=int(time.time()) - 3600)).json()["error_code"], 1060
        )
        self.assertEqual(self.stealth(1, timed("robot", 1)).json()["error_code"], 1048)
        self.assertIsNone(self.robot(1).signs_actions_since)

    def test_every_order_action_is_gated(self):
        """Every action the order endpoint accepts (upstream ones plus all Roblosats escrow_* and
        bond_* onchain actions, hybrids, mixta A-D, attach_chat) goes through the same gate before
        anything runs: unsigned -> 1059 for a robot whose app signs, a signature for another
        action -> 1048."""
        from api.serializers import UpdateOrderSerializer

        actions = list(UpdateOrderSerializer().fields["action"].choices)
        self.assertGreater(len(actions), 25)
        order = self.order(maker=1, taker=2, status=Order.Status.WF2)
        self.assertEqual(self.stealth(1, timed("stealth", 1)).status_code, 200)
        with (
            patch("api.views.escrow_flow.is_onchain", return_value=True),
            patch("api.views.escrow_flow.action") as flow_action,
            patch("api.views.Logics") as logics,
        ):
            for action in actions:
                extra = {"rating": "5"} if action == "rate_platform" else {}
                response = self.act(order.id, action, 1, **extra)
                self.assertEqual(response.status_code, 400, action)
                self.assertEqual(response.json().get("error_code"), 1059, action)
                other = "pause" if action != "pause" else "cancel"
                response = self.act(order.id, action, 1, order_sig(other, order.id, 1), **extra)
                self.assertEqual(response.json().get("error_code"), 1048, action)
            flow_action.assert_not_called()
            self.assertEqual(
                [c for c in logics.mock_calls if not c[0].startswith("order_lock")], []
            )
            # signed: the gate lets it through to escrow_flow (the reply GET then fails on the
            # mocked Logics, which does not matter here)
            self.client.raise_request_exception = False
            self.act(order.id, "escrow_keys", 1, order_sig("escrow_keys", order.id, 1))
            flow_action.assert_called_once()

    def test_take_variants_are_gated(self):
        order = self.order(maker=1)
        self.assertEqual(self.stealth(2, timed("stealth", 2)).status_code, 200)
        for extra in ({}, {"take_lightning": True}, {"take_onchain": True}):
            response = self.act(order.id, "take", 2, **extra)
            self.assertEqual(response.json().get("error_code"), 1059, extra)
        self.assertIsNone(Order.objects.get(id=order.id).taker)

    def test_dispute_actions(self):
        order = self.order(maker=1, taker=2, status=Order.Status.DIS)
        statement = "telegram: @someone \n\n " + "I paid the fiat and the seller does not answer. " * 4
        with patch("roblosats_admin.alerts.queue"):
            # old app: plain statement, unsigned
            self.client.raise_request_exception = False
            self.act(order.id, "submit_statement", 1, statement=statement)
            self.assertEqual(Order.objects.get(id=order.id).maker_statement, statement.strip())
            # new app: the action and the statement are both signed
            response = self.act(
                order.id, "submit_statement", 2, order_sig("submit_statement", order.id, 2),
                statement=statement,
            )
            self.assertEqual(response.json().get("error_code"), 1048)
            self.act(
                order.id, "submit_statement", 2, order_sig("submit_statement", order.id, 2),
                statement=sign_action(statement, 2),
            )
            self.assertEqual(Order.objects.get(id=order.id).taker_statement.strip(), statement.strip())
            # now robot 2 signs: an unsigned attach_chat for it is refused
            response = self.act(order.id, "attach_chat", 2, chat_log="[]")
            self.assertEqual(response.json().get("error_code"), 1059)

    def test_make_robot_settings_and_review(self):
        response = self.client.put(
            reverse("robot"), {"webhook_enabled": False, "signature": timed("robot", 1)},
            content_type="application/json", **auth(1),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNotNone(self.robot(1).signs_actions_since)
        response = self.client.put(
            reverse("robot"), {"webhook_enabled": False}, content_type="application/json", **auth(1)
        )
        self.assertEqual(response.json()["error_code"], 1059)
        pubkey = read_file("tests/robots/1/nostr_pubkey")
        response = self.client.post(reverse("review"), {"pubkey": pubkey}, **auth(1))
        self.assertEqual(response.json()["error_code"], 1059)
        response = self.client.post(reverse("make"), {"type": 0, "currency": 1, "amount": 100}, **auth(1))
        self.assertEqual(response.json()["error_code"], 1059)
        response = self.client.post(
            reverse("make"),
            {"type": 0, "currency": 1, "amount": 100, "signature": timed("make", 1, ts=0)},
            **auth(1),
        )
        self.assertEqual(response.json()["error_code"], 1060)


@patch("api.utils.REQUIRE_SIGNED_ACTIONS", True)
class EnforcedViewsTest(ViewsBase):
    def test_unsigned_refused_even_if_never_signed(self):
        order = self.order()
        self.assertEqual(self.act(order.id, "pause", 1).json()["error_code"], 1059)
        self.assertEqual(self.stealth(1).json()["error_code"], 1059)
        self.assertFalse(Notification.objects.filter(robot=self.robot(1)).exists())

    def test_signed_accepted(self):
        order = self.order()
        self.assertEqual(self.act(order.id, "pause", 1, order_sig("pause", order.id, 1)).status_code, 200)
        self.assertEqual(Order.objects.get(id=order.id).status, Order.Status.PAU)
        self.assertEqual(self.stealth(1, timed("stealth", 1)).status_code, 200)
