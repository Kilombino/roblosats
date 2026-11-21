"""Roblosats rl31-beta22: who gets what when a bond is slashed on an onchain or hybrid order.

Order #23 on Kilobot (hybrid B: onchain BUY, the seller took with Lightning): the buyer/maker
never gave its payout info, its onchain bond (3,330) was penalized (the penalty paid 1,830 to
the coordinator, 1,500 to the miners) and upstream's add_slashed_rewards "returned" 1,830 to
the FAULTY maker as rewards (maker bond bigger than the taker's) besides the taker's 750.

Rules checked here, for every fault path of hybrid A, hybrid B and pure onchain orders:
- the trader at fault gets nothing back;
- the innocent one gets SLASHED_BOND_REWARD_SPLIT of what the coordinator really got from the
  slashed bond, at most of its own bond;
- proceeds = what the coordinator got - the reward;
- a new penalty pays at most ESCROW_PENALTY_MAX_FEE_PCT of the bond in mining.
"""

import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from api.escrow import bonds, flow
from api.escrow.transactions import coins_spend_tx
from api.logics import LEDGER_TABLE, Logics
from api.models import Currency, LNPayment, OnchainBond, Order, TakeOrder

COORD_SPK = bytes.fromhex("00141ba581ae53fe6674a37da5ad40d660e1b974b658")  # p2wpkh, as on Kilobot


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


def _penalty_hex(value, rate=10.0):
    return coins_spend_tx([("ab" * 32, 0, value)], COORD_SPK, rate).serialize().hex()


def _fake_penalize(bond, reason=""):
    """bonds.penalize without a node: the bond is PENALIZED, its penalty_tx stays as signed."""
    if bond is None or bond.status != OnchainBond.Status.LOCKED:
        return False
    bond.status = OnchainBond.Status.PENALIZED
    bond.spend_txid = "cd" * 32
    bond.save(update_fields=["status", "spend_txid"])
    return True


def _ln(user, concept, sats, status=LNPayment.Status.LOCKED):
    now = timezone.now()
    return LNPayment.objects.create(
        concept=concept,
        type=LNPayment.Types.HOLD,
        sender=user,
        status=status,
        num_satoshis=sats,
        payment_hash=uuid.uuid4().hex,
        preimage=uuid.uuid4().hex,
        invoice=f"lnbcrt{uuid.uuid4().hex}",
        created_at=now,
        expires_at=now + timezone.timedelta(hours=3),
    )


class Base(TestCase):
    def setUp(self):
        self.patches = [
            patch("api.logics.send_notification"),
            patch("api.logics.nostr_send_order_event"),
            patch("api.logics.send_devfund_donation"),
            patch("api.tasks.send_devfund_donation"),
            patch("api.models.order.send_notification"),
            patch("api.escrow.bonds.penalize", side_effect=_fake_penalize),
            patch("api.logics.LNNode"),
            patch("api.escrow.flow.coordinator_fee_spk", return_value=COORD_SPK),
        ]
        mocks = [p.start() for p in self.patches]
        self.ln = mocks[6]
        self.ln.settle_hold_invoice.return_value = True
        self.ln.cancel_return_hold_invoice.return_value = True
        self.ln.double_check_htlc_is_settled.return_value = True
        User.objects.get_or_create(username="admin")
        self.currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _order(self, maker_sells, sats=50_000):
        now = timezone.now()
        self.maker, self.taker = _user(), _user()
        return Order.objects.create(
            currency=self.currency,
            type=Order.Types.SELL if maker_sells else Order.Types.BUY,
            status=Order.Status.PUB,
            escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now,
            expires_at=now + timezone.timedelta(hours=24),
            has_range=False,
            amount=100,
            satoshis=sats,
            t0_satoshis=sats,
            last_satoshis=sats,
            is_explicit=True,
            maker=self.maker,
            bond_size=3,
        )

    def _bond(self, order, owner, role, value, take_order=None):
        return OnchainBond.objects.create(
            order=order,
            take_order=take_order,
            owner=owner,
            role=role,
            owner_pubkey="02" + "11" * 32,
            coordinator_pubkey="02" + "22" * 32,
            refund_csv=144,
            address=f"bcrt1p{role}bond{uuid.uuid4().hex[:6]}",
            script_pubkey="5120" + "33" * 32,
            required_sats=value,
            txid=uuid.uuid4().hex * 2,
            vout=0,
            value=value,
            penalty_tx=_penalty_hex(value),
            status=OnchainBond.Status.LOCKED,
        )

    def _ln_take(self, order, taker_bond_sats=1_500):
        Logics.take(order, self.taker, lightning=True)
        take = TakeOrder.objects.get(order=order, taker=self.taker)
        take.taker_bond = _ln(self.taker, LNPayment.Concepts.TAKEBOND, taker_bond_sats)
        take.save(update_fields=["taker_bond"])
        with patch("api.logics.escrow_flow.on_contract"), patch("api.logics.MarketTick.log_a_tick"):
            Logics.finalize_contract(take)
        order.refresh_from_db()
        return order

    def _onchain_take(self, order, taker_bond_sats=3_330):
        order.taker = self.taker
        order.status = Order.Status.WFE
        order.save()
        return self._bond(order, self.taker, "taker", taker_bond_sats)

    def rewards(self, user):
        user.robot.refresh_from_db()
        return user.robot.earned_rewards

    def ledger(self, order):
        with connection.cursor() as c:
            c.execute(f"SELECT robot_id, kind, amount FROM {LEDGER_TABLE} WHERE order_id = %s ORDER BY id", [order.id])
            return c.fetchall()


class HybridB(Base):
    """Maker BUYS onchain (bond 3,330 → penalty pays 1,830), the seller takes with Lightning."""

    def setUp(self):
        super().setUp()
        self.order = self._order(maker_sells=False)
        self.bond = self._bond(self.order, self.maker, "maker", 3_330)
        self.order = self._ln_take(self.order)
        self.assertTrue(flow.has_proxy(self.order))
        self.order.trade_escrow = _ln(self.taker, LNPayment.Concepts.TRESCROW, 50_085)
        self.order.save()

    def test_order_23_buyer_without_payout_info(self):
        """WFI expiry, maker (buyer) at fault: nothing back to the maker; the taker gets half of
        min(1,830 received, 1,500 its bond) = 750; proceeds 1,830 - 750 = 1,080."""
        self.order.status = Order.Status.WFI
        self.order.save()
        Logics.order_expires(self.order)
        self.order.refresh_from_db()
        self.bond.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.EXP)
        self.assertEqual(self.bond.status, OnchainBond.Status.PENALIZED)
        self.assertEqual(bonds.penalty_sats(self.bond), 1_830)
        self.assertEqual(self.rewards(self.maker), 0)
        self.assertEqual(self.rewards(self.taker), 750)
        self.assertEqual(self.order.proceeds, 1_080)
        self.assertEqual(self.ledger(self.order), [(self.taker.robot.id, "slash_reward", 750)])
        self.assertEqual(self.order.taker_bond.status, LNPayment.Status.RETNED)
        self.assertEqual(self.order.trade_escrow.status, LNPayment.Status.RETNED)
        self.assertNotIn("returned", str(self.order.logs))

    def test_maker_cancels_before_escrow(self):
        self.order.status = Order.Status.WFE
        self.order.save()
        ok, _ = Logics.cancel_order(self.order, self.maker)
        self.assertTrue(ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.UCA)
        self.assertEqual(self.rewards(self.maker), 0)
        self.assertEqual(self.rewards(self.taker), 750)
        self.assertEqual(self.order.proceeds, 1_080)

    def test_taker_without_escrow_rewards_the_maker_and_goes_back_onchain(self):
        """WFE expiry, Lightning seller at fault: its 1,500 settled, maker gets 750, proceeds
        750, the order back in the book as onchain. Until beta21 the reward ran after the order
        lost its proxy and crashed/was refused."""
        self.order.status = Order.Status.WFE
        self.order.save()
        Logics.order_expires(self.order)
        self.order.refresh_from_db()
        self.bond.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PUB)
        self.assertEqual(self.order.escrow_type, Order.EscrowTypes.ONCHAIN)
        self.assertEqual(self.bond.status, OnchainBond.Status.LOCKED)
        self.assertEqual(self.rewards(self.maker), 750)
        self.assertEqual(self.rewards(self.taker), 0)
        self.assertEqual(self.order.proceeds, 750)

    def test_taker_cancels_before_escrow(self):
        self.order.status = Order.Status.WF2
        self.order.save()
        ok, _ = Logics.cancel_order(self.order, self.taker)
        self.assertTrue(ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PUB)
        self.assertEqual(self.rewards(self.maker), 750)
        self.assertEqual(self.rewards(self.taker), 0)
        self.assertEqual(self.order.proceeds, 750)

    def test_penalty_below_the_takers_bond_rewards_half_of_what_was_received(self):
        """A pricey penalty (received 1,000 < taker bond 1,500): reward 500, proceeds 500."""
        OnchainBond.objects.filter(pk=self.bond.pk).update(penalty_tx=_penalty_hex(3_330, rate=15.4))
        self.order.status = Order.Status.WFI
        self.order.save()
        Logics.order_expires(self.order)
        self.order.refresh_from_db()
        received = bonds.penalty_sats(OnchainBond.objects.get(pk=self.bond.pk))
        self.assertLess(received, 1_500)
        self.assertEqual(self.rewards(self.taker), received // 2)
        self.assertEqual(self.rewards(self.maker), 0)
        self.assertEqual(self.order.proceeds, received - received // 2)

    def test_penalty_not_broadcast_credits_nothing(self):
        self.order.status = Order.Status.WFI
        self.order.save()
        with patch("api.escrow.bonds.penalize", side_effect=bonds.EscrowError("node down")):
            Logics.order_expires(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.rewards(self.taker), 0)
        self.assertEqual(self.rewards(self.maker), 0)
        self.assertEqual(self.order.proceeds, 0)

    def test_repeat_credits_once(self):
        self.order.status = Order.Status.WFI
        self.order.save()
        Logics.order_expires(self.order)
        self.order.refresh_from_db()
        Logics.add_slashed_rewards(self.order, self.order.maker_bond, self.order.taker_bond)
        self.assertEqual(self.rewards(self.taker), 750)
        self.order.refresh_from_db()
        self.assertEqual(self.order.proceeds, 1_080)


class HybridA(Base):
    """Maker SELLS onchain (bond 3,330), the buyer takes with a Lightning bond of 1,500."""

    def setUp(self):
        super().setUp()
        self.order = self._order(maker_sells=True)
        self.bond = self._bond(self.order, self.maker, "maker", 3_330)
        self.order = self._ln_take(self.order)
        self.order.status = Order.Status.WFE
        self.order.save()

    def test_maker_at_fault(self):
        with patch("api.escrow.flow.funding_fault", return_value="seller"):
            Logics.order_expires(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.EXP)
        self.assertEqual(self.rewards(self.maker), 0)
        self.assertEqual(self.rewards(self.taker), 750)
        self.assertEqual(self.order.proceeds, 1_080)

    def test_taker_at_fault(self):
        with patch("api.escrow.flow.funding_fault", return_value="buyer"):
            Logics.order_expires(self.order)
        self.order.refresh_from_db()
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)
        self.assertEqual(self.rewards(self.maker), 750)
        self.assertEqual(self.rewards(self.taker), 0)
        self.assertEqual(self.order.proceeds, 750)

    def test_taker_cancels(self):
        ok, _ = Logics.cancel_order(self.order, self.taker)
        self.assertTrue(ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PUB)
        self.assertEqual(self.rewards(self.maker), 750)
        self.assertEqual(self.order.proceeds, 750)


class PureOnchain(Base):
    """Both bonds onchain, 3,330 each (penalties pay 1,830)."""

    def setUp(self):
        super().setUp()
        self.order = self._order(maker_sells=True)
        self.mbond = self._bond(self.order, self.maker, "maker", 3_330)
        self.tbond = self._onchain_take(self.order)

    def _expire(self, fault):
        with patch("api.escrow.flow.funding_fault", return_value=fault):
            Logics.order_expires(self.order)
        self.order.refresh_from_db()
        self.mbond.refresh_from_db()
        self.tbond.refresh_from_db()

    def test_seller_maker_without_deposit(self):
        self._expire("seller")
        self.assertEqual(self.mbond.status, OnchainBond.Status.PENALIZED)
        self.assertEqual(self.tbond.status, OnchainBond.Status.RETURNABLE)
        self.assertEqual(self.rewards(self.taker), 915)
        self.assertEqual(self.rewards(self.maker), 0)
        self.assertEqual(self.order.proceeds, 915)
        self.assertEqual(self.order.expiry_reason, Order.ExpiryReasons.NESCRO)

    def test_buyer_taker_without_payout_info(self):
        self._expire("buyer")
        self.assertEqual(self.tbond.status, OnchainBond.Status.PENALIZED)
        self.assertEqual(self.rewards(self.maker), 915)
        self.assertEqual(self.rewards(self.taker), 0)
        self.assertEqual(self.order.proceeds, 915)

    def test_smaller_innocent_bond_caps_the_reward(self):
        OnchainBond.objects.filter(pk=self.tbond.pk).update(value=1_000)
        self._expire("seller")
        self.assertEqual(self.rewards(self.taker), 500)
        self.assertEqual(self.order.proceeds, 1_330)

    def test_maker_cancels(self):
        ok, _ = Logics.cancel_order(self.order, self.maker)
        self.assertTrue(ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.UCA)
        self.assertEqual(self.rewards(self.taker), 915)
        self.assertEqual(self.rewards(self.maker), 0)
        self.assertEqual(self.order.proceeds, 915)

    def test_taker_cancels(self):
        ok, _ = Logics.cancel_order(self.order, self.taker)
        self.assertTrue(ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PUB)
        self.assertEqual(self.rewards(self.maker), 915)
        self.assertEqual(self.rewards(self.taker), 0)
        self.assertEqual(self.order.proceeds, 915)

    def test_expiry_twice_credits_once(self):
        self._expire("seller")
        flow.credit_penalty(self.order, "maker")
        self.assertEqual(self.rewards(self.taker), 915)
        self.order.refresh_from_db()
        self.assertEqual(self.order.proceeds, 915)


class LightningOrderUnchanged(Base):
    """Pure Lightning range order (upstream): the maker's bigger bond still gets its change back."""

    def test_upstream_range_return(self):
        order = self._order(maker_sells=True)
        order.escrow_type = Order.EscrowTypes.LIGHTNING
        order.maker_bond = _ln(self.maker, LNPayment.Concepts.MAKEBOND, 3_000, LNPayment.Status.SETLED)
        order.taker_bond = _ln(self.taker, LNPayment.Concepts.TAKEBOND, 1_000)
        order.taker = self.taker
        order.save()
        Logics.add_slashed_rewards(order, order.maker_bond, order.taker_bond)
        order.refresh_from_db()
        self.assertEqual(self.rewards(self.taker), 500)
        self.assertEqual(self.rewards(self.maker), 2_000)
        self.assertEqual(order.proceeds, 500)


class PenaltyFeeRate(TestCase):
    def _rate(self, value, n=1, env=None):
        env = env or {}
        with patch("api.escrow.service.config", side_effect=lambda k, cast=None, default=None: env.get(k, default)):
            return bonds.penalty_fee_rate(value, n, COORD_SPK)

    def test_minimum_bond_pays_at_most_ten_percent(self):
        rate = self._rate(3_330)
        tx = coins_spend_tx([("ab" * 32, 0, 3_330)], COORD_SPK, rate)
        fee = 3_330 - tx.outputs[0].value
        self.assertLessEqual(fee, 333)
        self.assertGreaterEqual(rate, 1.0)
        self.assertGreater(tx.outputs[0].value, 2_990)

    def test_big_bond_keeps_the_ceiling(self):
        self.assertEqual(self._rate(1_000_000), 10.0)

    def test_tiny_bond_gets_the_floor(self):
        self.assertEqual(self._rate(500), 1.0)

    def test_settings(self):
        env = {"ESCROW_PENALTY_FEE_RATE": 5.0, "ESCROW_PENALTY_MAX_FEE_PCT": 20.0, "ESCROW_PENALTY_MIN_FEE_RATE": 2.0}
        self.assertEqual(self._rate(3_330, env=env), 4.43)  # 4.44 would round to 667 > 666
        self.assertEqual(self._rate(100_000, env=env), 5.0)
        self.assertEqual(self._rate(1_000, env=env), 2.0)

    def test_several_coins_count_their_inputs(self):
        self.assertLess(self._rate(6_000, n=3), self._rate(6_000, n=1))

    def test_refresh_signs_the_capped_penalty(self):
        from api.escrow import bonds as b

        bond = OnchainBond(
            status=OnchainBond.Status.FUNDING, address="bcrt1pzz", required_sats=3_330, value=0,
        )
        with patch.object(b, "confirmed_coins", return_value=[("ef" * 32, 1, 3_330)]), \
                patch("api.escrow.flow.coordinator_fee_spk", return_value=COORD_SPK), \
                patch.object(OnchainBond, "save"):
            b.refresh(bond)
        self.assertEqual(bond.status, OnchainBond.Status.SIGN)
        self.assertGreaterEqual(b.penalty_sats(bond), 2_997)
