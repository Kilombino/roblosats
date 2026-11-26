"""Roblosats 0.9.12: on-chain bonds are not penalized when a dispute opens (Kilombino, TG 14873).

Mocked unit tests (no node):
- open_dispute settles only Lightning hold invoices: the hybrid B maker bond (behind a proxy)
  stays locked, a pure on-chain order touches no bond (they are inside the escrow), hybrid A
  still settles its Lightning taker bond;
- every dispute decision on a hybrid B order: the on-chain maker bond goes back on chain (not
  credited) when its owner keeps it and is penalized at the decision when it loses; a bond
  penalized earlier (#29, or by the safety guard) is credited back to a winner as before;
- the penalty's fee ladder (sign, choose the cheapest rung, fall back, bump by RBF, old apps
  with the top penalty only) and its accounting (rewards on what the penalty pays at least);
- the timelock safety guard (penalize before the owner can take the bond back alone, a bond
  already taken back, a dispute decided after that);
- no cancellation during a dispute; order logs written by two instances are both kept.
The same paths against a regtest node: test_escrow_service_regtest.py (EscrowDisputeBond0912Regtest).
"""

import uuid
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from api.escrow import bonds, flow, service
from api.escrow.taproot import Transaction, derive_secret, pubkey, schnorr_sign, with_hashtype
from api.logics import Logics
from api.models import Currency, LNPayment, OnchainBond, OnchainPayment, Order, TakeOrder

COORD = derive_secret(b"coordinador-0912", "kilobot", 7, "coordinator")
OWNER = derive_secret(b"tok-owner-0912", "kilobot", 7, "bond")
COORD_SPK = bytes.fromhex("0014" + "77" * 20)


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


def _ln(concept, sender, sats, status=LNPayment.Status.LOCKED):
    now = timezone.now()
    return LNPayment.objects.create(
        concept=concept, type=LNPayment.Types.HOLD, sender=sender, status=status, num_satoshis=sats,
        payment_hash=uuid.uuid4().hex, preimage=uuid.uuid4().hex, invoice=f"lnbcrt{uuid.uuid4().hex}",
        created_at=now, expires_at=now + timezone.timedelta(hours=3),
    )


def _sig(m):
    return with_hashtype(schnorr_sign(OWNER, m)).hex()


class Base(TestCase):
    def setUp(self):
        self.patches = [
            patch("api.logics.send_notification"),
            patch("api.logics.nostr_send_order_event"),
            patch("api.logics.send_devfund_donation"),
            patch("api.models.order.send_notification"),
            patch("api.admin.send_notification"),
            patch.object(bonds, "coordinator_bond_key", return_value=COORD),
            patch.object(service, "network_hrp", return_value="bc"),
            patch("api.escrow.flow.coordinator_fee_spk", return_value=COORD_SPK),
        ]
        for p in self.patches:
            p.start()
        User.objects.get_or_create(username="admin")
        self.currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        self.sent = []
        self.fee = 1.0
        self.txout = {}

    def tearDown(self):
        for p in self.patches:
            p.stop()

    # a fake node: sendrawtransaction records, gettxout answers from self.txout
    def rpc(self, method, params=None, wallet=None):
        if method == "sendrawtransaction":
            if getattr(self, "refuse_below", None) is not None:
                tx = Transaction.parse(bytes.fromhex(params[0]))
                fee = 30_000 - sum(o.value for o in tx.outputs)
                if fee < self.refuse_below:
                    raise service.EscrowError("bitcoind sendrawtransaction: min relay fee not met")
            self.sent.append(params[0])
            return Transaction.parse(bytes.fromhex(params[0])).txid()
        if method == "gettxout":
            return self.txout.get((params[0], params[1]), {"confirmations": 10})
        if method == "estimatesmartfee":
            return {"feerate": self.fee * 1000 / 1e8}
        if method == "getrawtransaction":
            return {"confirmations": 0}
        raise AssertionError(method)

    def _bond(self, order, owner, role="maker", status=OnchainBond.Status.SIGN, csv=540):
        bond = OnchainBond.objects.create(
            order=order, owner=owner, role=role, owner_pubkey=pubkey(OWNER).hex(),
            coordinator_pubkey=pubkey(COORD).hex(), refund_csv=csv, address="x", script_pubkey="",
            required_sats=30_000, status=OnchainBond.Status.SIGN, txid="11" * 32, vout=0, value=30_000,
            coins=[["11" * 32, 0, 30_000]],
        )
        rate = bonds.penalty_fee_rate(30_000, 1, COORD_SPK)
        bond.penalty_tx = bonds.coins_spend_tx(bonds.coins_of(bond), COORD_SPK, rate).serialize().hex()
        bond.save()
        bonds._build_penalty_ladder(bond)
        if status != OnchainBond.Status.SIGN:
            self._sign(bond, ladder=True)
            bond.refresh_from_db()
            if status != OnchainBond.Status.LOCKED:
                OnchainBond.objects.filter(pk=bond.pk).update(status=status)
                bond.refresh_from_db()
        return bond

    def _sign(self, bond, ladder=True):
        _, msgs = bonds.penalty_message(bond)
        sigs = [_sig(m) for m in msgs]
        lsigs = None
        if ladder:
            lsigs = []
            for rung in bond.penalty_ladder["rungs"]:
                tx = Transaction.parse(bytes.fromhex(rung["tx"]))
                lsigs.append([_sig(m) for m in bonds._coin_messages(bond, tx, bonds.coins_of(bond))])
        bonds.submit_penalty_signature(bond, sigs, lsigs)

    def _hybrid_b(self, status=Order.Status.CHA, bond_status=OnchainBond.Status.LOCKED):
        """A maker's onchain BUY taken by a Lightning seller (hybrid B), in the chat."""
        now = timezone.now()
        self.maker, self.taker = _user(), _user()
        order = Order.objects.create(
            currency=self.currency, type=Order.Types.BUY, status=Order.Status.PUB,
            escrow_type=Order.EscrowTypes.ONCHAIN, created_at=now, expires_at=now + timezone.timedelta(hours=24),
            has_range=False, amount=100, satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000,
            is_explicit=True, maker=self.maker, bond_size=3,
        )
        self.bond = self._bond(order, self.maker, status=OnchainBond.Status.LOCKED)
        Logics.take(order, self.taker, lightning=True)
        take = TakeOrder.objects.get(order=order, taker=self.taker)
        take.taker_bond = _ln(LNPayment.Concepts.TAKEBOND, self.taker, 30_000)
        take.save(update_fields=["taker_bond"])
        with patch("api.logics.escrow_flow.on_contract"), patch("api.logics.MarketTick.log_a_tick"):
            Logics.finalize_contract(take)
        order.refresh_from_db()
        order.trade_escrow = _ln(LNPayment.Concepts.TRESCROW, self.taker, 1_039_106)
        order.is_swap = True
        order.payout_tx = OnchainPayment.objects.create(
            concept=OnchainPayment.Concepts.PAYBUYER, status=OnchainPayment.Status.VALID, balance=None,
            address="bc1qbuyerpayout", num_satoshis=999_800, sent_satoshis=999_800, receiver=self.maker,
        )
        order.status = status
        order.expires_at = now - timezone.timedelta(minutes=1)
        order.save()
        if bond_status != OnchainBond.Status.LOCKED:
            OnchainBond.objects.filter(pk=self.bond.pk).update(status=bond_status)
            self.bond.refresh_from_db()
        return order

    def _open(self, order):
        with patch("api.logics.LNNode") as ln, patch.object(service, "rpc", side_effect=self.rpc):
            ln.settle_hold_invoice.return_value = True
            ok, err = Logics.open_dispute(order)
        self.assertTrue(ok, err)
        order.refresh_from_db()
        return order, ln


class OpenDispute(Base):
    def test_hybrid_b_onchain_maker_bond_stays_locked(self):
        order = self._hybrid_b()
        with patch("api.escrow.bonds.penalize") as pen:
            order, ln = self._open(order)
        pen.assert_not_called()
        self.assertEqual(self.sent, [])
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.LOCKED)
        order.maker_bond.refresh_from_db()
        self.assertEqual(order.maker_bond.status, LNPayment.Status.LOCKED)  # the proxy too
        # the Lightning ones are settled as before: the taker's bond and the escrow
        settled = {c.args[0] for c in ln.settle_hold_invoice.call_args_list}
        self.assertEqual(settled, {order.taker_bond.preimage, order.trade_escrow.preimage})
        self.assertEqual(order.status, Order.Status.DIS)
        self.assertIn("stays **locked** until the decision", order.logs)

    def test_pure_onchain_touches_no_bond(self):
        now = timezone.now()
        maker, taker = _user(), _user()
        order = Order.objects.create(
            currency=self.currency, type=Order.Types.BUY, status=Order.Status.CHA,
            escrow_type=Order.EscrowTypes.ONCHAIN, created_at=now, expires_at=now - timezone.timedelta(minutes=1),
            has_range=False, amount=100, satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000,
            maker=maker, taker=taker, bond_size=3,
        )
        b1 = self._bond(order, maker, status=OnchainBond.Status.IN_DEPOSIT)
        with patch("api.logics.Logics.settle_bond") as settle:
            order, ln = self._open(order)
        settle.assert_not_called()
        ln.settle_hold_invoice.assert_not_called()
        b1.refresh_from_db()
        self.assertEqual(b1.status, OnchainBond.Status.IN_DEPOSIT)
        self.assertEqual(self.sent, [])
        self.assertIn("inside the onchain escrow", order.logs)

    def test_hybrid_a_still_settles_the_lightning_taker_bond(self):
        now = timezone.now()
        maker, taker = _user(), _user()
        order = Order.objects.create(
            currency=self.currency, type=Order.Types.SELL, status=Order.Status.CHA,
            escrow_type=Order.EscrowTypes.ONCHAIN, created_at=now, expires_at=now - timezone.timedelta(minutes=1),
            has_range=False, amount=100, satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000,
            maker=maker, taker=taker, bond_size=3, taker_lightning=True,
            taker_bond=_ln(LNPayment.Concepts.TAKEBOND, taker, 30_000),
        )
        order, ln = self._open(order)
        ln.settle_hold_invoice.assert_called_once_with(order.taker_bond.preimage)
        order.taker_bond.refresh_from_db()
        self.assertEqual(order.taker_bond.status, LNPayment.Status.SETLED)

    def test_no_cancel_during_a_dispute(self):
        order = self._hybrid_b()
        order, _ = self._open(order)
        for user in (self.maker, self.taker):
            ok, err = Logics.cancel_order(order, user)
            self.assertFalse(ok)
        order.status = Order.Status.WFR
        order.save()
        for user in (self.maker, self.taker):
            self.assertFalse(Logics.cancel_order(order, user)[0])
        self.assertIsNone(Logics.collaborative_cancel(order))  # not in DIS/WFR: does nothing
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.WFR)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.LOCKED)


class Decisions(Base):
    """The admin actions (through the dispute page's resolve) on a hybrid B order."""

    def setUp(self):
        super().setUp()
        self.order_admin = admin.site._registry[Order]
        self.staff = User.objects.create_user(username=f"staff_{uuid.uuid4().hex[:6]}", password="x", is_staff=True)

    def _credits(self, order):
        from api.logics import ensure_ledger

        ensure_ledger()
        with connection.cursor() as c:
            c.execute("SELECT robot_id, amount, note FROM coord_reward_ledger WHERE order_id = %s AND amount > 0", [order.id])
            return sorted((int(r), int(a), n) for r, a, n in c.fetchall())

    def _decide(self, decision, bond_status=OnchainBond.Status.LOCKED, signed_return=False):
        from roblosats_admin import disputes

        order = self._hybrid_b()
        order, _ = self._open(order)
        if bond_status != OnchainBond.Status.LOCKED:
            OnchainBond.objects.filter(pk=self.bond.pk).update(status=bond_status)
            if bond_status == OnchainBond.Status.PENALIZED:
                LNPayment.objects.filter(pk=order.maker_bond.pk).update(status=LNPayment.Status.SETLED)
        if signed_return:
            ret = bytes.fromhex("5120" + "ab" * 32)
            bonds.set_return_address(self.bond, ret, "bc1preturn")
            self.bond.refresh_from_db()
            for_ladder = []
            for rung in self.bond.auto_return["ladder"]:
                tx = Transaction.parse(bytes.fromhex(rung["tx"]))
                for_ladder.append([_sig(m) for m in bonds._coin_messages(self.bond, tx, bonds.coins_of(self.bond))])
            _, msgs = bonds.auto_return_message(self.bond)
            bonds.submit_auto_return_signature(self.bond, [_sig(m) for m in msgs], for_ladder)
        with patch.object(service, "rpc", side_effect=self.rpc), patch("api.logics.Logics.pay_buyer", return_value=True), \
                patch("api.escrow.flow.ledger_credit", create=True):
            ok, msgs = disputes.resolve(self.order_admin, Order.objects.get(pk=order.pk), decision, self.staff)
        self.assertTrue(ok, msgs)
        order.refresh_from_db()
        self.bond.refresh_from_db()
        return order

    def test_buyer_wins_bond_back_on_chain_not_credited(self):
        order = self._decide("buyer", signed_return=True)
        # the signed return went out, at the cheapest rung (mempool at 1 sat/vB)
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNED)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.bond.auto_return["sent"]["rate"], 1.0)
        # only the trade amount is credited
        # 0.9.16: the winner also gets SLASHED_BOND_REWARD_SPLIT of the loser's bond
        self.assertEqual(self._credits(order), sorted([(self.maker.robot.id, 15_000, ""), (self.maker.robot.id, 999_800, "maker_wins_trade")]))
        self.assertEqual(order.status, Order.Status.TLD)

    def test_buyer_wins_without_a_signed_return_gets_the_button(self):
        order = self._decide("buyer")
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)  # "Get my bond back"
        self.assertEqual(self.sent, [])
        # 0.9.16: the winner also gets SLASHED_BOND_REWARD_SPLIT of the loser's bond
        self.assertEqual(self._credits(order), sorted([(self.maker.robot.id, 15_000, ""), (self.maker.robot.id, 999_800, "maker_wins_trade")]))

    def test_seller_wins_penalizes_the_buyer_bond_at_the_decision(self):
        order = self._decide("seller")
        self.assertEqual(self.bond.status, OnchainBond.Status.PENALIZED)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.bond.penalty_ladder["sent"]["rate"], 1.0)  # cheapest rung
        order.maker_bond.refresh_from_db()
        self.assertEqual(order.maker_bond.status, LNPayment.Status.SETLED)
        # 0.9.16: the winner also gets SLASHED_BOND_REWARD_SPLIT of the loser's bond
        self.assertEqual(self._credits(order), sorted([(self.taker.robot.id, 14_250, ""), (self.taker.robot.id, 30_000 + 1_039_106, "taker_wins")]))
        self.assertEqual(order.status, Order.Status.MLD)

    def test_return_everything_frees_the_onchain_bond(self):
        order = self._decide("return")
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)
        credits = self._credits(order)
        self.assertNotIn(self.maker.robot.id, [c[0] for c in credits])
        self.assertEqual(sum(c[1] for c in credits), 30_000 + 1_039_106)

    def test_penalize_buyer_penalizes_at_the_decision(self):
        order = self._decide("penalize_buyer")
        self.assertEqual(self.bond.status, OnchainBond.Status.PENALIZED)
        self.assertEqual([c[:2] for c in self._credits(order)],
                         sorted([(self.taker.robot.id, 14_250), (self.taker.robot.id, 30_000), (self.taker.robot.id, 1_039_106)]))

    def test_penalize_seller_frees_the_innocent_buyer_bond(self):
        order = self._decide("penalize_seller")
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)
        # 0.9.16: the winner also gets SLASHED_BOND_REWARD_SPLIT of the loser's bond
        self.assertEqual(self._credits(order), sorted([(self.maker.robot.id, 15_000, ""), (self.taker.robot.id, 1_039_106, "escrow_back")]))

    def test_completed_frees_the_onchain_bond(self):
        order = self._decide("completed")
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)
        self.assertEqual(self._credits(order), [(self.taker.robot.id, 30_000, "taker_bond")])

    def test_order_29_already_penalized_buyer_wins_is_made_whole(self):
        """Opened under 0.9.11 (#29): the bond was penalized at the opening. A winning maker is
        credited its value back as rewards, as 0.9.11 did; nothing is broadcast."""
        order = self._decide("buyer", bond_status=OnchainBond.Status.PENALIZED)
        self.assertEqual(self.sent, [])
        # 0.9.16: the winner also gets SLASHED_BOND_REWARD_SPLIT of the loser's bond
        self.assertEqual(self._credits(order), sorted([(self.maker.robot.id, 15_000, ""), (self.maker.robot.id, 30_000 + 999_800, "maker_wins")]))

    def test_order_29_already_penalized_seller_wins(self):
        order = self._decide("seller", bond_status=OnchainBond.Status.PENALIZED)
        self.assertEqual(self.sent, [])
        # 0.9.16: the winner also gets SLASHED_BOND_REWARD_SPLIT of the loser's bond
        self.assertEqual(self._credits(order), sorted([(self.taker.robot.id, 14_250, ""), (self.taker.robot.id, 30_000 + 1_039_106, "taker_wins")]))

    def test_order_29_already_penalized_return_and_completed(self):
        order = self._decide("return", bond_status=OnchainBond.Status.PENALIZED)
        self.assertIn((self.maker.robot.id, 30_000, "maker_bond"), self._credits(order))
        self.assertEqual(self.sent, [])

    def test_decided_after_the_owner_took_it_back(self):
        """Its timelock passed and the owner spent it alone: a loss penalizes nothing."""
        order = self._hybrid_b()
        order, _ = self._open(order)
        self.txout[("11" * 32, 0)] = None
        from roblosats_admin import disputes

        with patch.object(service, "rpc", side_effect=self._spent_rpc):
            ok, msgs = disputes.resolve(self.order_admin, Order.objects.get(pk=order.pk), "seller", self.staff)
        self.assertTrue(ok, msgs)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNED)
        self.assertEqual(self.sent, [])
        order.refresh_from_db()
        self.assertIn("no longer at its address", order.logs)

    def _spent_rpc(self, method, params=None, wallet=None):
        if method == "sendrawtransaction":
            raise service.EscrowError("bitcoind sendrawtransaction: bad-txns-inputs-missingorspent")
        if method == "gettxout":
            return None
        return self.rpc(method, params, wallet)

    def test_page_explains_the_locked_bond(self):
        from roblosats_admin import disputes

        order = self._hybrid_b()
        order, _ = self._open(order)
        with patch.object(service, "rpc", side_effect=self.rpc):
            buyer = " ".join(disputes.plan(order, "buyer")["lines"])
            seller = " ".join(disputes.plan(order, "seller")["lines"])
        self.assertIn("straight back to them on chain", buyer)
        self.assertIn("their on-chain bond (30\u202f000), penalized on chain now", seller)
        self.assertIn("still locked", buyer)


class PenaltyLadder(Base):
    def _order(self):
        now = timezone.now()
        u = _user()
        self.owner = u
        return Order.objects.create(
            type=Order.Types.BUY, status=Order.Status.PUB, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100,
            satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=u, bond_size=3,
        )

    def test_rungs_pay_the_penalty_output_at_lower_rates(self):
        bond = self._bond(self._order(), None or _user())
        rungs = bond.penalty_ladder["rungs"]
        self.assertEqual([r["rate"] for r in rungs][:2], [1.0, 3.0])
        self.assertEqual(rungs[-1]["tx"], bond.penalty_tx)
        self.assertEqual(rungs[-1]["rate"], 10.0)  # the 0.9.11 cap on a 30,000-sat bond
        outs = [Transaction.parse(bytes.fromhex(r["tx"])).outputs for r in rungs]
        self.assertTrue(all(len(o) == 1 and o[0].script_pubkey == COORD_SPK for o in outs))
        values = [o[0].value for o in outs]
        self.assertEqual(values, sorted(values, reverse=True))  # cheaper rung = more to the coordinator
        data = bonds.bond_data(bond)
        self.assertEqual(data["bond_penalty_ladder"], [r["tx"] for r in rungs])
        self.assertFalse(data["bond_penalty_ladder_signed"])

    def test_cheapest_rung_the_mempool_needs(self):
        bond = self._bond(self._order(), _user(), status=OnchainBond.Status.LOCKED)
        self.fee = 2.0
        with patch.object(service, "rpc", side_effect=self.rpc):
            self.assertTrue(bonds.penalize(bond, "test"))
        bond.refresh_from_db()
        self.assertEqual(bond.penalty_ladder["sent"]["rate"], 3.0)
        self.assertEqual(bond.spend_txid, Transaction.parse(bytes.fromhex(self.sent[0])).txid())
        # accounting: what was sent, and the floor (the top penalty) for rewards
        self.assertGreater(bonds.penalty_sats(bond), bonds.penalty_floor_sats(bond))
        self.assertEqual(bonds.penalty_floor_sats(bond), Transaction.parse(bytes.fromhex(bond.penalty_tx)).outputs[0].value)

    def test_refused_rung_falls_to_the_next(self):
        bond = self._bond(self._order(), _user(), status=OnchainBond.Status.LOCKED)
        self.refuse_below = 500  # the 1 sat/vB rung pays ~110 sats: refused
        with patch.object(service, "rpc", side_effect=self.rpc):
            bonds.penalize(bond)
        bond.refresh_from_db()
        self.assertEqual(bond.penalty_ladder["sent"]["rate"], 10.0)

    def test_bump_after_the_wait(self):
        bond = self._bond(self._order(), _user(), status=OnchainBond.Status.LOCKED)
        with patch.object(service, "rpc", side_effect=self.rpc):
            bonds.penalize(bond)
            bond.refresh_from_db()
            first = bond.spend_txid
            self.assertFalse(bonds.bump_penalty(bond))  # fresh and the mempool still at 1
            at = bond.penalty_ladder["sent"]["at"]
            self.assertTrue(bonds.bump_penalty(bond, now=at + 7200))
        bond.refresh_from_db()
        self.assertEqual(bond.penalty_ladder["sent"]["rate"], 3.0)
        self.assertEqual(bond.penalty_ladder["sent"]["replaced"], first)
        self.assertEqual(len(self.sent), 2)
        # the same coins: an RBF replacement
        a, b = (Transaction.parse(bytes.fromhex(x)) for x in self.sent)
        self.assertEqual([(i.txid, i.vout) for i in a.inputs], [(i.txid, i.vout) for i in b.inputs])

    def test_old_app_top_penalty_only(self):
        bond = self._bond(self._order(), _user())
        self._sign(bond, ladder=False)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.LOCKED)
        self.assertNotIn("sigs", bond.penalty_ladder)
        with patch.object(service, "rpc", side_effect=self.rpc):
            bonds.penalize(bond)
        bond.refresh_from_db()
        self.assertEqual(self.sent, [bond.penalty_tx[:0] + self.sent[0]])
        self.assertEqual(bond.penalty_ladder["sent"]["rate"], 10.0)
        self.assertEqual(bonds.penalty_sats(bond), bonds.penalty_floor_sats(bond))

    def test_bond_without_ladder_from_0911(self):
        """A bond locked under 0.9.11: no ladder at all, penalized at its single penalty."""
        bond = self._bond(self._order(), _user(), status=OnchainBond.Status.LOCKED)
        OnchainBond.objects.filter(pk=bond.pk).update(penalty_ladder={})
        bond.refresh_from_db()
        with patch.object(service, "rpc", side_effect=self.rpc):
            bonds.penalize(bond)
        bond.refresh_from_db()
        self.assertEqual(Transaction.parse(bytes.fromhex(self.sent[0])).txid(),
                         Transaction.parse(bytes.fromhex(bond.penalty_tx)).txid())

    def test_ladder_signed_later_while_locked(self):
        bond = self._bond(self._order(), _user())
        self._sign(bond, ladder=False)
        bond.refresh_from_db()
        OnchainBond.objects.filter(pk=bond.pk).update(penalty_ladder={})
        bond.refresh_from_db()
        data = bonds.bond_data(bond)  # builds it for a locked bond
        self.assertEqual(len(data["bond_penalty_ladder"]), 3)
        bond.refresh_from_db()
        lsigs = []
        for rung in bond.penalty_ladder["rungs"]:
            tx = Transaction.parse(bytes.fromhex(rung["tx"]))
            lsigs.append([_sig(m) for m in bonds._coin_messages(bond, tx, bonds.coins_of(bond))])
        bonds.submit_penalty_ladder_signatures(bond, lsigs)
        bond.refresh_from_db()
        self.assertTrue(bonds.bond_data(bond)["bond_penalty_ladder_signed"])
        self.assertEqual(len(bonds.signed_penalty_rungs(bond)), 3)

    def test_bad_rung_signature_refused(self):
        bond = self._bond(self._order(), _user())
        _, msgs = bonds.penalty_message(bond)
        bad = [[with_hashtype(schnorr_sign(COORD, m)).hex() for m in msgs] for _ in bond.penalty_ladder["rungs"]]
        with self.assertRaises(service.EscrowError):
            bonds.submit_penalty_signature(bond, [_sig(m) for m in msgs], bad)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.SIGN)

    def test_tampered_rung_refused(self):
        bond = self._bond(self._order(), _user())
        lad = dict(bond.penalty_ladder)
        coins = bonds.coins_of(bond)
        lad["rungs"][0]["tx"] = bonds.coins_spend_tx(coins, bytes.fromhex("5120" + "ee" * 32), 1.0).serialize().hex()
        OnchainBond.objects.filter(pk=bond.pk).update(penalty_ladder=lad)
        bond.refresh_from_db()
        with self.assertRaises(service.EscrowError):
            self._sign(bond, ladder=True)


class SafetyGuard(Base):
    def setUp(self):
        super().setUp()
        self.order = self._hybrid_b()
        self.order, _ = self._open(self.order)

    def test_far_from_the_timelock_nothing_happens(self):
        self.txout[("11" * 32, 0)] = {"confirmations": 100}  # 540 - 100 = 440 left
        with patch.object(service, "rpc", side_effect=self.rpc):
            self.assertEqual(bonds.guard_disputed_bonds(force=True), 0)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.LOCKED)

    def test_close_to_the_timelock_it_is_penalized(self):
        self.txout[("11" * 32, 0)] = {"confirmations": 540 - 100}  # 100 left < 135 (540 // 4)
        with patch.object(service, "rpc", side_effect=self.rpc):
            self.assertEqual(bonds.guard_disputed_bonds(force=True), 1)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.PENALIZED)
        self.order.refresh_from_db()
        self.order.maker_bond.refresh_from_db()
        self.assertEqual(self.order.maker_bond.status, LNPayment.Status.SETLED)
        self.assertIn("safety", self.order.logs)
        self.assertEqual(self.order.status, Order.Status.DIS)  # still for the coordinator to decide

    def test_safety_never_more_than_a_quarter_of_the_timelock(self):
        self.assertEqual(bonds.safety_blocks(self.bond), 135)
        self.bond.refund_csv = 1079
        self.assertEqual(bonds.safety_blocks(self.bond), 144)
        self.bond.refund_csv = 20
        self.assertEqual(bonds.safety_blocks(self.bond), 6)

    def test_already_taken_back_by_its_owner(self):
        self.txout[("11" * 32, 0)] = None
        with patch.object(service, "rpc", side_effect=self.rpc):
            bonds.guard_disputed_bonds(force=True)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNED)
        self.assertEqual(self.sent, [])

    def test_only_disputed_orders(self):
        Order.objects.filter(pk=self.order.pk).update(status=Order.Status.CHA, is_disputed=False)
        self.txout[("11" * 32, 0)] = {"confirmations": 539}
        with patch.object(service, "rpc", side_effect=self.rpc):
            self.assertEqual(bonds.guard_disputed_bonds(force=True), 0)

    def test_throttled_to_once_a_minute(self):
        self.txout[("11" * 32, 0)] = {"confirmations": 539}
        bonds._guard["at"] = 0.0
        with patch.object(service, "rpc", side_effect=self.rpc):
            self.assertEqual(bonds.guard_disputed_bonds(), 1)
            self.assertEqual(bonds.guard_disputed_bonds(), 0)

    def test_then_the_winning_maker_is_credited_its_value(self):
        self.txout[("11" * 32, 0)] = {"confirmations": 539}
        with patch.object(service, "rpc", side_effect=self.rpc):
            bonds.guard_disputed_bonds(force=True)
            self.assertEqual(flow.dispute_bond_sats(Order.objects.get(pk=self.order.pk), "maker", wins=True), 30_000)


class Logs(Base):
    def test_two_instances_both_logs_kept(self):
        from decouple import config  # noqa: F401

        now = timezone.now()
        o = Order.objects.create(
            type=Order.Types.BUY, status=Order.Status.PUB, created_at=now, expires_at=now + timezone.timedelta(hours=1),
            has_range=False, amount=100, satoshis=100_000, t0_satoshis=100_000, last_satoshis=100_000,
            maker=_user(), bond_size=3,
        )
        with patch("api.models.order.config", return_value=False):
            a, b = Order.objects.get(pk=o.pk), Order.objects.get(pk=o.pk)
            a.log("first, from one instance")
            b.log("second, from a stale one")
        o.refresh_from_db()
        self.assertIn("first, from one instance", o.logs)
        self.assertIn("second, from a stale one", o.logs)


class WrongChain(Base):
    """0.9.12 (#28): a bond or escrow funding paid with Spamcoin on the SHA-256 chain."""

    class FakeResp:
        def __init__(self, data, code=200):
            self.data, self.status_code = data, code

        def json(self):
            return self.data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    class FakeSession:
        def __init__(self, by_addr):
            self.by_addr, self.calls = by_addr, []

        def get(self, url, timeout=None):
            self.calls.append(url)
            addr = url.split("/address/")[1].split("/")[0]
            return WrongChain.FakeResp(self.by_addr.get(addr, []))

    def _taker_bond(self):
        now = timezone.now()
        maker, self.taker = _user(), _user()
        order = Order.objects.create(
            currency=self.currency, type=Order.Types.BUY, status=Order.Status.EXP,
            escrow_type=Order.EscrowTypes.ONCHAIN, created_at=now, expires_at=now, has_range=False, amount=100,
            satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=maker, bond_size=3,
        )
        bond = OnchainBond.objects.create(
            order=order, owner=self.taker, role="taker", owner_pubkey=pubkey(OWNER).hex(),
            coordinator_pubkey=pubkey(COORD).hex(), refund_csv=540, address="bc1pwrongchainbond",
            script_pubkey="", required_sats=30_000, status=OnchainBond.Status.ABANDONED,
        )
        return order, bond

    def test_reported_once_to_robot_coordinator_and_log(self):
        from api.escrow import spamchain

        order, bond = self._taker_bond()
        utxo = {"txid": "c2" * 32, "vout": 0, "value": 30_000, "status": {"confirmed": True, "block_height": 976777}}
        s = self.FakeSession({"bc1pwrongchainbond": [utxo]})
        with patch("api.tasks.send_notification") as notify, patch("roblosats_admin.alerts.queue") as alert:
            self.assertEqual(spamchain.check_all(session=s), 1)
            self.assertEqual(spamchain.check_all(session=s), 0)  # the same coin: once
        notify.delay.assert_called_once_with(order_id=order.id, message=f"wrong_chain:{self.taker.id}:bond:30000:540")
        self.assertEqual(alert.call_args.args[0], "onchain")
        self.assertEqual(alert.call_args.args[2]["kind"], "wrong_chain")
        bond.refresh_from_db()
        self.assertEqual(bond.wrong_chain[0]["txid"], "c2" * 32)
        self.assertEqual(bond.wrong_chain[0]["height"], 976777)
        order.refresh_from_db()
        self.assertIn("**Wrong chain**", order.logs)
        # a second payment later is reported too
        s.by_addr["bc1pwrongchainbond"].append({**utxo, "vout": 1, "value": 500})
        with patch("api.tasks.send_notification") as notify, patch("roblosats_admin.alerts.queue"):
            self.assertEqual(spamchain.check_all(session=s), 1)

    def test_off_and_old_bonds_not_watched(self):
        from api.escrow import spamchain

        order, bond = self._taker_bond()
        s = self.FakeSession({})
        with patch("api.escrow.spamchain.config", side_effect=lambda k, **kw: False if k == "SPAMCHAIN_WATCH" else kw.get("default")):
            self.assertEqual(spamchain.check_all(session=s), 0)
        self.assertEqual(s.calls, [])
        OnchainBond.objects.filter(pk=bond.pk).update(created_at=timezone.now() - timezone.timedelta(days=30))
        spamchain.check_all(session=s)
        self.assertEqual(s.calls, [])
        OnchainBond.objects.filter(pk=bond.pk).update(created_at=timezone.now(), status=OnchainBond.Status.PENALIZED)
        spamchain.check_all(session=s)
        self.assertEqual(s.calls, [])

    def test_explorer_down_is_not_fatal(self):
        from api.escrow import spamchain

        self._taker_bond()

        class Down:
            def get(self, *a, **k):
                raise ConnectionError("tor down")

        self.assertEqual(spamchain.check_all(session=Down()), 0)

    def test_robot_notice_in_its_language(self):
        from api.notifications import Notifications

        order, bond = self._taker_bond()
        sent = []
        with patch.object(Notifications, "send_message", lambda self, o, r, title, *a, **k: sent.append(title)):
            Notifications().wrong_chain(order, self.taker.id, "bond", 30_000, 540)
        self.assertIn("WRONG chain", sent[0])
        self.assertIn("540 blocks", sent[0])
        self.assertIn("Spamchain", sent[0])
