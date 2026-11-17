"""Roblosats rl31-beta15: onchain fee plan 2.

Kilombino, 17 nov 2026: a collaborative cancel must refund the coordinator fee, as with
Lightning orders. The fees now ride inside the escrow output and only the release of a
completed trade pays the coordinator (one output of at least ESCROW_MIN_COORDINATOR_FEE_SATS
for the whole order, hybrid A included); a cancel or a "return everything" dispute gives each
trader its fee back; a dispute with a winner pays the coordinator as when the fees were paid in
the deposit. Escrows created before (no "fee_plan" in funding_signatures) keep plan 1.

Mocked (no node): deposit planning, the payouts of every release, proceeds and the summary.
"""

import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from api import order_summary
from api.escrow import flow
from api.escrow.taproot import Transaction, TxIn, TxOut
from api.escrow.transactions import Utxo, plan_deposit
from api.models import Currency, OnchainBond, OnchainEscrow, Order
from api.tests.test_escrow_transactions import SPK, _setup

SELLER_SPK = "5120" + "a1" * 32
BUYER_SPK = "5120" + "b2" * 32
COORD_SPK = "5120" + "c3" * 32
DEV_SPK = "5120" + "d4" * 32


class FeesPlan2(SimpleTestCase):
    def _fees(self, split, at_release=False, floor="1000", version=None):
        with patch("api.escrow.service.fee_split", return_value=split), \
             patch("api.escrow.flow.buyer_fee_at_release", return_value=at_release), \
             patch.dict("os.environ", {"ESCROW_MIN_COORDINATOR_FEE_SATS": floor}):
            return flow.fees(object(), version=version)

    def test_hybrid_a_one_floor_for_the_whole_order(self):
        s, b = self._fees((17, 136), at_release=True)
        self.assertEqual(s + b, 1000)
        self.assertEqual((s, b), self._fees((17, 136)))

    def test_plan_1_hybrid_kept_for_old_escrows(self):
        self.assertEqual(self._fees((17, 136), at_release=True, version=1), (1000, 1000))

    def test_below_dust_without_floor_not_charged(self):
        self.assertEqual(self._fees((17, 136), floor="0"), (0, 0))
        self.assertEqual(self._fees((170, 1360), floor="0"), (170, 1360))

    def test_deposit_keeps_the_fees_in_the_escrow_output(self):
        c, fs = _setup()
        us = [Utxo("aa" * 32, 0, 120_000, fs)]
        ub = [Utxo("bb" * 32, 1, 3_000, SPK(9))]
        plan = plan_deposit(us, ub, c, 100_000, 860, 140, SPK(1), 500, SPK(2), fs, SPK(9), 2.0,
                            fees_in_escrow=True)
        self.assertEqual(plan.tx.outputs[0].value, 100_000 + 1000)
        self.assertFalse([o for o in plan.tx.outputs if o.script_pubkey in (SPK(1), SPK(2))])
        self.assertEqual(plan.seller_pays - plan.seller_mining, 100_000 + 860)
        self.assertEqual(plan.buyer_pays - plan.buyer_mining, 140)


def _user():
    return User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")


class Plan2Base(TestCase):
    """The payouts of every release of a plan-2 escrow: trade 100,000, bonds 3,000 each (the
    buyer's 1,700 left after its fee of 1,000 and 300 of mining came out of it), seller fee
    1,000, buyer fee 1,000."""

    def setUp(self):
        User.objects.get_or_create(username="admin")
        self.currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        self.seller, self.buyer = _user(), _user()
        self.captured = []
        self.patches = [
            patch("api.escrow.flow.coordinator_fee_spk", return_value=bytes.fromhex(COORD_SPK)),
            patch("api.escrow.service.start_release", side_effect=self._start_release),
            patch("api.logics.Logics.return_bond"),
            patch("api.models.order.send_notification"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _start_release(self, escrow, payouts, kind, fee_payer=0):
        self.captured.append((list(payouts), kind))
        outs = [TxOut(p.value - (300 if i == 0 else 0), p.script_pubkey) for i, p in enumerate(payouts)]
        escrow.release_kind = kind
        escrow.release_tx = Transaction([TxIn("dd" * 32, 0)], outs).serialize().hex()
        escrow.status = OnchainEscrow.Status.RELEASING
        escrow.save()
        return Transaction([TxIn("dd" * 32, 0)], outs)

    def _order(self, status=Order.Status.CHA, hybrid=False):
        now = timezone.now()
        o = Order.objects.create(
            currency=self.currency, type=Order.Types.SELL, status=status, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100,
            satoshis=100_000, t0_satoshis=100_000, last_satoshis=100_000, is_explicit=True,
            maker=self.seller, taker=self.buyer, bond_size=3, taker_lightning=hybrid,
        )
        return o

    def _bond(self, o, owner, role):
        return OnchainBond.objects.create(
            order=o, owner=owner, role=role, owner_pubkey="02" + "11" * 32, coordinator_pubkey="02" + "22" * 32,
            refund_csv=144, address=f"bcrt1p{role}", script_pubkey="5120" + "33" * 32, required_sats=3_000,
            txid=uuid.uuid4().hex * 2, vout=0, value=3_000, status=OnchainBond.Status.IN_DEPOSIT,
        )

    def _escrow(self, o, version=2, hybrid=False):
        sb = self._bond(o, self.seller, "maker")
        plan = {"trade_sats": 100_000, "seller_fee": 1_000, "seller_mining": 250}
        if hybrid:
            plan.update(bond_ids={"seller": sb.pk}, bond_in_escrow={"seller": 3_000}, buyer_fee=1_000,
                        buyer_fee_at_release=1_000)
            held = {"seller": 1_000, "buyer": 0}
        else:
            bb = self._bond(o, self.buyer, "taker")
            plan.update(bond_ids={"seller": sb.pk, "buyer": bb.pk}, bond_in_escrow={"seller": 3_000, "buyer": 1_700},
                        buyer_fee=1_000, buyer_mining=300, buyer_fee_from_bond=True)
            held = {"seller": 1_000, "buyer": 1_000}
        sigs = {"plan": plan, "seller_payout_spk": SELLER_SPK, "buyer_payout_spk": BUYER_SPK,
                "seller_payout_address": "s", "buyer_payout_address": "b"}
        if version >= 2:
            sigs["fee_plan"] = 2
            plan["fees_in_escrow"] = held
        out = 100_000 + sum(plan["bond_in_escrow"].values()) + (sum(held.values()) if version >= 2 else 0)
        return OnchainEscrow.objects.create(
            order=o, status=OnchainEscrow.Status.LOCKED, escrow_sats=out, funding_txid="ee" * 32,
            funding_signatures=sigs, coordinator_fee_sats=0 if version >= 2 else 2_000,
        )

    def _outs(self):
        payouts, kind = self.captured[-1]
        return {p.script_pubkey.hex(): p.value for p in payouts}, kind

    def _released(self, escrow):
        escrow.refresh_from_db()
        escrow.status = OnchainEscrow.Status.RELEASED
        escrow.release_txid = "ff" * 32
        escrow.save()


class ReleasesPlan2(Plan2Base):
    def test_success_pays_the_coordinator_in_the_release(self):
        o = self._order()
        escrow = self._escrow(o)
        flow.seller_confirms(o)
        outs, kind = self._outs()
        self.assertEqual(kind, OnchainEscrow.ReleaseKind.COOPERATIVE)
        self.assertEqual(outs, {BUYER_SPK: 100_000 + 1_700, SELLER_SPK: 3_000, COORD_SPK: 2_000})
        self.assertEqual(sum(outs.values()), escrow.escrow_sats)
        self._released(escrow)
        self.assertEqual(flow.proceeds(o), 2_000)
        self.assertEqual(flow.charged_fees(escrow), {"seller": 1_000, "buyer": 1_000})

    def test_collaborative_cancel_refunds_both_fees(self):
        o = self._order()
        escrow = self._escrow(o)
        flow.collaborative_cancel(o)
        outs, _ = self._outs()
        self.assertEqual(outs, {SELLER_SPK: 100_000 + 3_000 + 1_000, BUYER_SPK: 1_700 + 1_000})
        self.assertNotIn(COORD_SPK, outs)
        self._released(escrow)
        self.assertEqual(flow.proceeds(o), 0)
        self.assertEqual(flow.charged_fees(escrow), {"seller": 0, "buyer": 0})

    def test_return_everything_is_a_collaborative_cancel(self):
        o = self._order(status=Order.Status.DIS)
        escrow = self._escrow(o)
        flow.return_everything(o)
        outs, kind = self._outs()
        self.assertEqual(kind, OnchainEscrow.ReleaseKind.SELLER_WINS)
        self.assertEqual(outs, {SELLER_SPK: 104_000, BUYER_SPK: 2_700})
        self._released(escrow)
        self.assertEqual(flow.charged_fees(escrow), {"seller": 0, "buyer": 0})

    def test_dispute_winner_gets_as_before_and_the_coordinator_the_fees(self):
        o = self._order(status=Order.Status.DIS)
        self._escrow(o)
        with patch("api.escrow.flow.ln_bond_dispute_credit"):
            flow.resolve_dispute(o, buyer_wins=True)
        outs, _ = self._outs()
        # winner: trade + its bond + half the loser's bond, as in plan 1
        self.assertEqual(outs[BUYER_SPK], 100_000 + 1_700 + 1_500)
        self.assertEqual(outs[COORD_SPK], 1_500 + 2_000)
        e = flow.escrow_of(o)
        self.assertEqual(e.funding_signatures["dispute_coordinator_sats"], 1_500)
        self.assertEqual(e.funding_signatures["release_fee_sats"], 2_000)

    def test_dev_share_out_of_the_coordinator_fees(self):
        o = self._order()
        self._escrow(o)
        with patch("api.escrow.flow.dev_fee", return_value=(200, bytes.fromhex(DEV_SPK))):
            flow.seller_confirms(o)
        outs, _ = self._outs()
        self.assertEqual(outs[COORD_SPK], 1_800)
        self.assertEqual(outs[DEV_SPK], 200)
        e = flow.escrow_of(o)
        self.assertEqual(e.funding_signatures["release_dev_sats"], 200)
        self.assertEqual(flow.dev_paid(e), 200)

    def test_hybrid_a_one_coordinator_output(self):
        o = self._order(hybrid=True)
        escrow = self._escrow(o, hybrid=True)
        with patch("api.escrow.flow.buyer_fee_at_release", return_value=True):
            flow.seller_confirms(o)
        outs, _ = self._outs()
        self.assertEqual(outs, {BUYER_SPK: 100_000 - 1_000, SELLER_SPK: 3_000, COORD_SPK: 2_000})
        e = flow.escrow_of(o)
        self.assertEqual(e.funding_signatures["release_coordinator_sats"], 1_000)
        self._released(escrow)
        self.assertEqual(flow.proceeds(o), 2_000)

    def test_hybrid_a_cancel_charges_nothing(self):
        o = self._order(hybrid=True)
        self._escrow(o, hybrid=True)
        flow.collaborative_cancel(o)
        outs, _ = self._outs()
        self.assertEqual(outs, {SELLER_SPK: 100_000 + 3_000 + 1_000})

    def test_plan_1_escrow_unchanged(self):
        o = self._order()
        escrow = self._escrow(o, version=1)
        self.assertEqual(flow.fee_plan(escrow), 1)
        flow.collaborative_cancel(o)
        outs, _ = self._outs()
        self.assertEqual(outs, {SELLER_SPK: 103_000, BUYER_SPK: 1_700})
        self._released(escrow)
        self.assertEqual(flow.charged_fees(escrow), {"seller": 1_000, "buyer": 1_000})

    def test_order_data_tells_the_client(self):
        o = self._order()
        escrow = self._escrow(o)
        flow.collaborative_cancel(o)
        with patch("api.escrow.flow.funding_leftover", return_value=[]), \
             patch("api.escrow.flow.waiting_for", return_value=(None, None)), \
             patch("api.escrow.flow.recommended_funding", return_value=(1_000, 1_500)), \
             patch("api.escrow.service.network_hrp", return_value="bcrt"), \
             patch("api.escrow.service.coordinator_alias", return_value="lab"), \
             patch("api.escrow.service.aggregate_nonce", return_value=""), \
             patch("api.escrow.flow.bond_role", side_effect=flow.EscrowError("x")):
            d = flow.order_data(o, self.buyer)
        self.assertEqual(d["escrow_fee_plan"], 2)
        self.assertEqual(d["escrow_fees_in_escrow"], {"seller": 1_000, "buyer": 1_000})
        self.assertTrue(d["escrow_release_cancel"])
        self.assertEqual(d["escrow_release_coordinator_sats"], 0)
        self.assertEqual(sum(d["escrow_bond_in_escrow"].values()) + d["escrow_trade_sats"] + 2_000, escrow.escrow_sats)


class SummaryPlan2(Plan2Base):
    def test_cancel_summary_shows_the_fee_returned(self):
        o = self._order(status=Order.Status.CCA)
        escrow = self._escrow(o)
        flow.collaborative_cancel(o)
        self._released(escrow)
        seller = order_summary.final_summary(o, self.seller)
        self.assertEqual(seller["coordinator_fee_sats"], 0)
        self.assertEqual(seller["fee_refunded_sats"], 1_000)
        self.assertEqual(seller["returned_sats"], 100_000 - 300)
        self.assertEqual(seller["bond_returned_sats"], 3_000)
        buyer = order_summary.final_summary(o, self.buyer)
        self.assertEqual(buyer["coordinator_fee_sats"], 0)
        self.assertEqual(buyer["fee_refunded_sats"], 1_000)
        self.assertFalse(buyer["fee_from_bond"])
        self.assertEqual(buyer["bond_returned_sats"], 1_700)

    def test_success_summary_charges_the_fees(self):
        o = self._order(status=Order.Status.SUC)
        escrow = self._escrow(o)
        flow.seller_confirms(o)
        self._released(escrow)
        o.status = Order.Status.SUC
        o.save()
        seller = order_summary.final_summary(o, self.seller)
        self.assertEqual(seller["coordinator_fee_sats"], 1_000)
        self.assertEqual(seller["fee_refunded_sats"], 0)
        buyer = order_summary.final_summary(o, self.buyer)
        self.assertEqual(buyer["coordinator_fee_sats"], 1_000)
        self.assertTrue(buyer["fee_from_bond"])
        self.assertEqual(buyer["received_sats"], 100_000 - 300)
