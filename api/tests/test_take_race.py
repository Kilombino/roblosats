"""0.9.8: the race between takers of an onchain order (mocked, no node).

An onchain taker waits for the confirmation of its bond; another taker (a Lightning bond locks
in seconds) can lock first. The loser's bond is freed, never penalized, its robot is told and
its order page shows only its bond. The flow against a node is in test_escrow_service_regtest.py
(EscrowTakeRaceRegtest)."""

import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api.escrow import flow
from api.escrow.service import EscrowError
from api.models import Currency, OnchainBond, Order, TakeOrder


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


class RaceBase(TestCase):
    def setUp(self):
        self.patches = [
            patch("api.logics.send_notification"),
            patch("api.logics.nostr_send_order_event"),
            patch("api.models.order.send_notification"),
            patch("api.escrow.flow.service.coordinator_alias", return_value="kilobot"),
            patch("api.escrow.flow.service.network_hrp", return_value="bcrt"),
            patch("api.escrow.bonds.service.network_hrp", return_value="bcrt"),
            patch("api.escrow.bonds.service.coordinator_alias", return_value="kilobot"),
            patch("api.escrow.bonds.confirmed_coins", return_value=[]),
            patch("api.escrow.flow._unconfirmed", return_value=[]),
        ]
        for p in self.patches:
            p.start()
        self.currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        now = timezone.now()
        self.maker, self.winner, self.loser = _user(), _user(), _user()
        self.order = Order.objects.create(
            currency=self.currency, type=Order.Types.SELL, status=Order.Status.WF2,
            escrow_type=Order.EscrowTypes.ONCHAIN, created_at=now, expires_at=now + timezone.timedelta(hours=24),
            has_range=False, amount=100, satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000,
            is_explicit=True, maker=self.maker, taker=self.winner, bond_size=3,
        )
        self.take = TakeOrder.objects.create(
            order=self.order, taker=self.loser, expires_at=now - timezone.timedelta(seconds=1)
        )
        self.bond = OnchainBond.objects.create(
            order=self.order, take_order=self.take, owner=self.loser, role="taker",
            owner_pubkey="02" + "11" * 32, coordinator_pubkey="02" + "22" * 32, refund_csv=144,
            address="bcrt1ploserbond", script_pubkey="5120" + "33" * 32, required_sats=30_000,
            status=OnchainBond.Status.ABANDONED,
        )

    def tearDown(self):
        for p in self.patches:
            p.stop()


class LostTakeBond(RaceBase):
    def test_the_loser_owns_its_bond(self):
        self.assertEqual(flow.lost_take_bond(self.order, self.loser), self.bond)
        self.assertEqual(flow.bond_role(self.order, self.loser)[0], "taker")

    def test_not_for_maker_taker_or_strangers(self):
        for u in (self.maker, self.winner, _user()):
            self.assertIsNone(flow.lost_take_bond(self.order, u))

    def test_not_while_its_take_is_alive(self):
        self.take.expires_at = timezone.now() + timezone.timedelta(hours=1)
        self.take.save(update_fields=["expires_at"])
        self.assertIsNone(flow.lost_take_bond(self.order, self.loser))

    def test_no_new_bond_for_a_take_that_is_over(self):
        with self.assertRaises(EscrowError):
            flow.bond_action(self.order, self.loser, "bond_key", {"bond_pubkey": "02" + "44" * 32})
        self.assertEqual(OnchainBond.objects.filter(owner=self.loser).count(), 1)


class LostTakeView(RaceBase):
    def test_only_its_bond(self):
        self.bond.status = OnchainBond.Status.RETURNABLE
        self.bond.save(update_fields=["status"])
        d = flow.lost_take_view(self.order, self.loser)
        self.assertTrue(d["lost_take"])
        self.assertTrue(d["lost_take_taken"])
        self.assertFalse(d["lost_take_done"])
        self.assertFalse(d["is_participant"])
        self.assertEqual(d["bond_status_code"], OnchainBond.Status.RETURNABLE)
        self.assertEqual(d["bond_address"], "bcrt1ploserbond")
        for k in ("maker_nick", "escrow_address", "taker_nick", "description", "payment_method"):
            self.assertNotIn(k, d)

    def test_unpaid_bond_waiting_in_the_mempool(self):
        with patch("api.escrow.flow._unconfirmed", return_value=[("ab" * 32, 30_000)]):
            d = flow.lost_take_view(self.order, self.loser)
        self.assertFalse(d["lost_take_done"])
        self.assertEqual(d["bond_mempool_txid"], "ab" * 32)

    def test_never_paid_is_done(self):
        self.assertTrue(flow.lost_take_view(self.order, self.loser)["lost_take_done"])

    def test_never_paid_on_a_public_order_is_the_public_order(self):
        self.order.status = Order.Status.PUB
        self.order.taker = None
        self.order.save(update_fields=["status", "taker"])
        self.assertIsNone(flow.lost_take_view(self.order, self.loser))

    def test_returned_is_done(self):
        self.bond.status = OnchainBond.Status.RETURNED
        self.bond.spend_txid = "cd" * 32
        self.bond.save(update_fields=["status", "spend_txid"])
        d = flow.lost_take_view(self.order, self.loser)
        self.assertTrue(d["lost_take_done"])
        self.assertEqual(d["bond_spend_txid"], "cd" * 32)


class LoseRace(RaceBase):
    def test_frees_and_notifies_onchain_losers_only(self):
        self.take.expires_at = timezone.now() + timezone.timedelta(hours=1)
        self.take.save(update_fields=["expires_at"])
        self.bond.status = OnchainBond.Status.FUNDING
        self.bond.save(update_fields=["status"])
        ln = TakeOrder.objects.create(order=self.order, taker=_user(), lightning=True,
                                      expires_at=timezone.now() + timezone.timedelta(hours=1))
        win = TakeOrder.objects.create(order=self.order, taker=self.winner,
                                       expires_at=timezone.now() + timezone.timedelta(hours=1))
        with patch("api.tasks.send_notification") as notify, \
                patch("api.escrow.bonds.refresh", side_effect=lambda b: b):
            flow.lose_race(self.order, win)
        self.bond.refresh_from_db()
        self.take.refresh_from_db()
        ln.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.ABANDONED)
        self.assertLessEqual(self.take.expires_at, timezone.now())
        self.assertGreater(ln.expires_at, timezone.now())  # Lightning pretaker: upstream rules
        notify.delay.assert_called_once_with(order_id=self.order.id, message=f"take_lost:{self.loser.id}")

    def test_never_penalized(self):
        self.bond.status = OnchainBond.Status.SIGN
        self.bond.txid = "ef" * 32
        self.bond.save(update_fields=["status", "txid"])
        flow.release_take_bond(self.take)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)

    def test_case_b_expiry_frees_by_the_take(self):
        """The order became a Lightning order (case B): the take's bond is still freed."""
        from api.logics import Logics

        self.order.escrow_type = Order.EscrowTypes.LIGHTNING
        self.order.save(update_fields=["escrow_type"])
        self.bond.status = OnchainBond.Status.FUNDING
        self.bond.save(update_fields=["status"])
        with patch("api.escrow.bonds.refresh", side_effect=lambda b: b):
            Logics.take_order_expires(self.take)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.ABANDONED)


class TakeLostNotice(RaceBase):
    def test_sent_to_the_loser_in_its_language(self):
        from api.notifications import Notifications

        self.loser.robot.telegram_lang_code = "es"
        self.loser.robot.save(update_fields=["telegram_lang_code"])
        with patch.object(Notifications, "send_message") as send:
            Notifications().take_lost(self.order, self.loser.id)
        robot, title = send.call_args.args[1], send.call_args.args[2]
        self.assertEqual(robot, self.loser.robot)
        self.assertIn(f"orden {self.order.id}", title)
        self.assertIn("no pierdes nada", title)
        self.assertTrue(send.call_args.kwargs["action"])

    def test_not_to_the_trader_of_the_order(self):
        from api.notifications import Notifications

        with patch.object(Notifications, "send_message") as send:
            Notifications().take_lost(self.order, self.winner.id)
        send.assert_not_called()

    def test_task_routes_take_lost(self):
        from api.notifications import Notifications
        from api.tasks import send_notification

        with patch.object(Notifications, "take_lost") as tl:
            send_notification(order_id=self.order.id, message=f"take_lost:{self.loser.id}")
        tl.assert_called_once()
        self.assertEqual(tl.call_args.args[1], self.loser.id)


class SameBlock(RaceBase):
    """Kilombino: "what if two takers confirm in the same block?" Both bonds are LOCKED when
    the loop looks at them: the robot that took the order first wins, the other loses the race."""

    def test_oldest_take_wins_when_both_lock_in_the_same_pass(self):
        from api.logics import Logics

        self.order.status = Order.Status.PUB
        self.order.taker = None
        self.order.save(update_fields=["status", "taker"])
        TakeOrder.objects.filter(order=self.order).delete()
        later = timezone.now() + timezone.timedelta(hours=1)
        first, second = _user(), _user()
        # The younger take's row is written first (explicit ids): without order_by("id") the
        # database hands it out first and it would win.
        base = (TakeOrder.objects.order_by("-id").values_list("id", flat=True).first() or 0) + 10
        t2 = TakeOrder.objects.create(id=base + 1, order=self.order, taker=second, expires_at=later)
        t1 = TakeOrder.objects.create(id=base, order=self.order, taker=first, expires_at=later)
        bonds = {}
        for t, k in ((t1, "44"), (t2, "55")):
            bonds[t.pk] = OnchainBond.objects.create(
                order=self.order, take_order=t, owner=t.taker, role="taker",
                owner_pubkey="02" + k * 32, coordinator_pubkey="02" + "22" * 32, refund_csv=144,
                address=f"bcrt1p{k}", script_pubkey="5120" + k * 32, required_sats=30_000,
                status=OnchainBond.Status.LOCKED, txid=k * 32, vout=0, value=30_000,
            )
        with patch("api.tasks.send_notification") as notify, \
                patch("api.escrow.flow.on_contract"), \
                patch("api.escrow.bonds.refresh", side_effect=lambda b, *a, **k: b), \
                patch("api.escrow.bonds._broadcast_auto_return"), \
                patch.object(Logics, "satoshis_now", return_value=1_000_000):
            flow._advance_bonds(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.WF2)
        self.assertEqual(self.order.taker, first)
        b1, b2 = (OnchainBond.objects.get(pk=bonds[t.pk].pk) for t in (t1, t2))
        self.assertEqual(b1.status, OnchainBond.Status.LOCKED)  # the contract's bond
        self.assertEqual(b2.status, OnchainBond.Status.RETURNABLE)  # free, never penalized
        notify.delay.assert_called_once_with(order_id=self.order.id, message=f"take_lost:{second.id}")
