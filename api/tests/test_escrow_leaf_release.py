"""Roblosats 0.9.5: the cooperative release in one signature.

Up to 0.9.4 the cooperative release (trade or collaborative cancel) was a MuSig2 key-path spend
of seller and buyer in two rounds: each app sends a nonce, then each one a partial signature.
Each trader had to open the order at least once after the other's nonce (order #24 on Kilobot:
"signed it automatically" after only the nonce). 0.9.5: the seller alone signs the same release
through leaf 0 (<seller> CHECKSIGVERIFY <coordinator> CHECKSIG); the coordinator adds its
signature and broadcasts. The buyer signs nothing. Works also on a release already started
with the two-round signing (same transaction, same outputs) as long as its fee is relayable.

Mixed orders where the buyer took with Lightning (case A, hybrid_plan 1): the buyer has nothing
in the escrow, so the escrow uses rescue plan 1 and the buyer signs nothing onchain.
"""

import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api.escrow import bip327 as M
from api.escrow import flow, service
from api.escrow.taproot import (
    SIGHASH_ALL_UNIFIED,
    EscrowContract,
    Transaction,
    TxOut,
    derive_secret,
    pubkey,
    schnorr_sign,
    schnorr_verify,
    with_hashtype,
    xonly,
)
from api.escrow.transactions import VB_KEYPATH, VB_LEAF_2SIG, release_tx
from api.models import OnchainEscrow, Order

BUYER_SPK = bytes.fromhex("5120" + "b2" * 32)
SELLER_SPK = bytes.fromhex("0014" + "a1" * 20)
COORD_SECRET = derive_secret(b"secreto-del-coordinador", "kilobot", 4242, "coordinator")


def _user():
    return User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")


def _order(buyer, seller, **extra):
    now = timezone.now()
    fields = dict(
        type=Order.Types.BUY,
        status=Order.Status.PAY,
        escrow_type=Order.EscrowTypes.ONCHAIN,
        created_at=now,
        expires_at=now + timezone.timedelta(hours=24),
        has_range=False,
        amount=100,
        satoshis=1_000_000,
        t0_satoshis=1_000_000,
        last_satoshis=1_000_000,
        maker=buyer,
        taker=seller,
        bond_size=3,
    )
    fields.update(extra)
    return Order.objects.create(**fields)


class LeafReleaseBase(TestCase):
    ESCROW_SATS = 1_060_000

    def setUp(self):
        self.buyer, self.seller = _user(), _user()
        self.order = _order(self.buyer, self.seller)
        self.ks = derive_secret(b"tok-seller", "kilobot", self.order.id, "seller")
        self.kb = derive_secret(b"tok-buyer", "kilobot", self.order.id, "buyer")
        self.patches = [
            patch.object(service, "coordinator_key", return_value=COORD_SECRET),
            patch.object(service, "network_hrp", return_value="bc"),
        ]
        for p in self.patches:
            p.start()
        self.sent = []

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _escrow(self, leaf_planned=True, rate=3.0, cancel=False, seller_leaf=True):
        fs = {"fee_plan": 2, "rescue_plan": 2, "extra_plan": 1}
        if seller_leaf:
            fs["seller_leaf_release"] = True
        if cancel:
            fs["cancel"] = True
        e = OnchainEscrow.objects.create(
            order=self.order,
            status=OnchainEscrow.Status.RELEASING,
            seller_pubkey=pubkey(self.ks).hex(),
            buyer_pubkey=pubkey(self.kb).hex(),
            coordinator_pubkey=pubkey(COORD_SECRET).hex(),
            rescue_csv=2016,
            escrow_sats=self.ESCROW_SATS,
            funding_txid="11" * 32,
            funding_vout=0,
            release_kind=OnchainEscrow.ReleaseKind.COOPERATIVE,
            funding_signatures=fs,
        )
        c = service.contract_for(e)
        vb = VB_LEAF_2SIG if leaf_planned else VB_KEYPATH
        payouts = [TxOut(1_030_000, BUYER_SPK), TxOut(30_000, SELLER_SPK)]
        tx = release_tx(c, e.funding_txid, 0, e.escrow_sats, payouts, vb, rate, 0)
        e.release_tx = tx.serialize().hex()
        e.release_fee_rate = rate
        e.address = c.address
        e.script_pubkey = c.script_pubkey.hex()
        e.save()
        return e

    def _leaf_sig(self, escrow, key=None):
        _, msg = service.cooperative_leaf_message(escrow)
        return with_hashtype(schnorr_sign(key or self.ks, msg)).hex()

    def _rpc(self, method, params=None, wallet=None):
        if method == "sendrawtransaction":
            self.sent.append(params[0])
            return Transaction.parse(bytes.fromhex(params[0])).txid()
        raise AssertionError(f"unexpected rpc {method}")


class CooperativeLeafRelease(LeafReleaseBase):
    def test_seller_alone_finishes_the_release_through_leaf_0(self):
        e = self._escrow()
        before = Transaction.parse(bytes.fromhex(e.release_tx))
        with patch.object(service, "rpc", side_effect=self._rpc):
            e = service.submit_cooperative_leaf(e, "seller", self._leaf_sig(e))
        self.assertEqual(e.status, OnchainEscrow.Status.RELEASED)
        self.assertEqual(len(self.sent), 1)
        tx = Transaction.parse(bytes.fromhex(self.sent[0]))
        # same outputs as the release both traders checked
        self.assertEqual([(o.value, o.script_pubkey) for o in tx.outputs],
                         [(o.value, o.script_pubkey) for o in before.outputs])
        self.assertEqual(tx.txid(), e.release_txid)
        c = service.contract_for(e)
        w = tx.inputs[0].witness
        self.assertEqual(len(w), 4)
        self.assertEqual(w[2], c.leaves[EscrowContract.SELLER_COORDINATOR])
        self.assertEqual(w[3], c.control_block(EscrowContract.SELLER_COORDINATOR))
        _, msg = service.cooperative_leaf_message(e)
        # stack: coordinator signature consumed last (CHECKSIG), seller first (CHECKSIGVERIFY)
        self.assertTrue(schnorr_verify(msg, xonly(pubkey(COORD_SECRET)), w[0][:64]))
        self.assertTrue(schnorr_verify(msg, xonly(pubkey(self.ks)), w[1][:64]))
        self.assertEqual(w[0][64], SIGHASH_ALL_UNIFIED)
        self.assertEqual(e.funding_signatures["release_path"], "seller_leaf")
        # the fee it was planned for: at least the rate, through the leaf
        fee = e.escrow_sats - sum(o.value for o in tx.outputs)
        self.assertGreaterEqual(fee / tx.vsize(), 3.0)

    def test_collaborative_cancel_returns_through_the_seller_leaf(self):
        e = self._escrow(cancel=True)
        with patch.object(service, "rpc", side_effect=self._rpc):
            e = service.submit_cooperative_leaf(e, "seller", self._leaf_sig(e))
        self.assertEqual(e.status, OnchainEscrow.Status.REFUNDED)

    def test_the_buyer_cannot_use_the_seller_leaf(self):
        e = self._escrow()
        with patch.object(service, "rpc", side_effect=self._rpc):
            with self.assertRaises(service.EscrowError):
                service.submit_cooperative_leaf(e, "buyer", self._leaf_sig(e, self.kb))
        self.assertEqual(self.sent, [])

    def test_a_wrong_signature_is_refused(self):
        e = self._escrow()
        _, keypath_msg = service.release_message(e)
        bad = [
            with_hashtype(schnorr_sign(self.kb, service.cooperative_leaf_message(e)[1])).hex(),  # buyer key
            with_hashtype(schnorr_sign(self.ks, keypath_msg)).hex(),  # key-path message
            schnorr_sign(self.ks, service.cooperative_leaf_message(e)[1]).hex(),  # no hash type
        ]
        with patch.object(service, "rpc", side_effect=self._rpc):
            for sig in bad:
                with self.assertRaises(service.EscrowError):
                    service.submit_cooperative_leaf(e, "seller", sig)
        self.assertEqual(self.sent, [])
        e.refresh_from_db()
        self.assertEqual(e.status, OnchainEscrow.Status.RELEASING)
        self.assertEqual(e.release_txid, "")

    def test_a_dispute_release_is_not_a_cooperative_one(self):
        e = self._escrow()
        e.release_kind = OnchainEscrow.ReleaseKind.BUYER_WINS
        e.save()
        with patch.object(service, "rpc", side_effect=self._rpc):
            with self.assertRaises(service.EscrowError):
                service.submit_cooperative_leaf(e, "seller", self._leaf_sig(e))

    def test_node_refusal_leaves_the_release_as_it_was(self):
        e = self._escrow()
        tx_before = e.release_tx

        def refuse(method, params=None, wallet=None):
            raise service.EscrowError("bitcoind sendrawtransaction: boom")

        with patch.object(service, "rpc", side_effect=refuse):
            with self.assertRaises(service.EscrowError):
                service.submit_cooperative_leaf(e, "seller", self._leaf_sig(e))
        e.refresh_from_db()
        self.assertEqual(e.status, OnchainEscrow.Status.RELEASING)
        self.assertEqual(e.release_tx, tx_before)
        self.assertEqual(e.release_txid, "")
        self.assertNotIn("release_path", e.funding_signatures)


class MidMusigRelease(LeafReleaseBase):
    """Order #24: a release started before 0.9.5 (planned for the key path), the seller's nonce
    already in, the buyer never came back. The seller's new app finishes it through its leaf."""

    def _nonce(self, e, role, key):
        c = service.contract_for(e)
        _, msg = service.release_message(e)
        i = service.ROLES.index(role)
        sec, pub = M.nonce_gen(key, c.musig_pubkeys()[i], c.output_key, msg, None)
        return sec, pub

    def test_leaf_finishes_a_release_with_only_the_sellers_nonce(self):
        e = self._escrow(leaf_planned=False, rate=2.987, seller_leaf=False)
        _, pub = self._nonce(e, "seller", self.ks)
        e = service.submit_release_nonce(e, "seller", pub.hex())
        self.assertTrue(service.leaf_release_ok(e))
        before = Transaction.parse(bytes.fromhex(e.release_tx))
        with patch.object(service, "rpc", side_effect=self._rpc):
            e = service.submit_cooperative_leaf(e, "seller", self._leaf_sig(e))
        self.assertEqual(e.status, OnchainEscrow.Status.RELEASED)
        tx = Transaction.parse(bytes.fromhex(self.sent[0]))
        self.assertEqual([(o.value, o.script_pubkey) for o in tx.outputs],
                         [(o.value, o.script_pubkey) for o in before.outputs])
        fee = e.escrow_sats - sum(o.value for o in tx.outputs)
        self.assertGreaterEqual(fee / tx.vsize(), service.MIN_RELEASE_RATE)
        # the buyer's late nonce or partial is refused: the release is gone
        _, bpub = self._nonce(e, "buyer", self.kb)
        with self.assertRaises(service.EscrowError):
            service.submit_release_nonce(e, "buyer", bpub.hex())

    def test_leaf_finishes_a_release_where_the_buyer_already_signed(self):
        e = self._escrow(leaf_planned=False, rate=2.987, seller_leaf=False)
        c = service.contract_for(e)
        _, msg = service.release_message(e)
        secs = {}
        for role, key in (("seller", self.ks), ("buyer", self.kb)):
            secs[role], pub = self._nonce(e, role, key)
            e = service.submit_release_nonce(e, role, pub.hex())
        ctx = M.SessionContext(bytes.fromhex(service.aggregate_nonce(e)), c.musig_pubkeys(), [c.tweak], [True], msg)
        e = service.submit_release_partial(e, "buyer", M.sign(secs["buyer"], self.kb, ctx).hex())
        self.assertEqual(e.status, OnchainEscrow.Status.RELEASING)
        # the seller's app lost its secret nonce (restart): no second round needed any more
        with patch.object(service, "rpc", side_effect=self._rpc):
            e = service.submit_cooperative_leaf(e, "seller", self._leaf_sig(e))
        self.assertEqual(e.status, OnchainEscrow.Status.RELEASED)
        with self.assertRaises(service.EscrowError):
            service.submit_release_partial(e, "seller", M.sign(secs["seller"], self.ks, ctx).hex())

    def test_a_keypath_release_too_cheap_for_the_leaf_stays_two_round(self):
        # 1.2 sat/vB for the key path is below 1 sat/vB through the bigger leaf witness
        e = self._escrow(leaf_planned=False, rate=1.2, seller_leaf=False)
        self.assertLess(service.leaf_release_rate(e), service.MIN_RELEASE_RATE)
        self.assertFalse(service.leaf_release_ok(e))
        with patch.object(service, "rpc", side_effect=self._rpc):
            with self.assertRaises(service.EscrowError):
                service.submit_cooperative_leaf(e, "seller", self._leaf_sig(e))
        self.assertEqual(self.sent, [])

    def test_the_old_two_round_signing_still_works(self):
        e = self._escrow(leaf_planned=False, rate=2.987, seller_leaf=False)
        c = service.contract_for(e)
        _, msg = service.release_message(e)
        secs = {}
        for role, key in (("seller", self.ks), ("buyer", self.kb)):
            secs[role], pub = self._nonce(e, role, key)
            e = service.submit_release_nonce(e, role, pub.hex())
        ctx = M.SessionContext(bytes.fromhex(service.aggregate_nonce(e)), c.musig_pubkeys(), [c.tweak], [True], msg)
        with patch.object(service, "rpc", side_effect=self._rpc):
            for role, key in (("seller", self.ks), ("buyer", self.kb)):
                e = service.submit_release_partial(e, role, M.sign(secs[role], key, ctx).hex())
        self.assertEqual(e.status, OnchainEscrow.Status.RELEASED)
        self.assertEqual(len(Transaction.parse(bytes.fromhex(self.sent[0])).inputs[0].witness), 1)


class LeafPlanning(LeafReleaseBase):
    def test_leaf_planned_release_pays_its_rate_through_the_leaf(self):
        e = self._escrow(leaf_planned=True, rate=3.0)
        self.assertGreaterEqual(service.leaf_release_rate(e), 3.0)

    def test_keypath_planned_release_pays_less_through_the_leaf(self):
        e = self._escrow(leaf_planned=False, rate=3.0)
        self.assertLess(service.leaf_release_rate(e), 3.0)

    def test_seller_leaf_flag_from_the_keys(self):
        e = self._escrow(seller_leaf=False)
        self.assertFalse(flow.seller_leaf(e))
        e.funding_signatures = {**e.funding_signatures, "seller_leaf_release": True}
        self.assertTrue(flow.seller_leaf(e))

    def test_waiting_for_with_a_leaf_capable_seller(self):
        e = self._escrow(seller_leaf=True)
        self.assertEqual(flow.waiting_for(self.order, e, "seller"), ("you", "release_signature"))
        self.assertEqual(flow.waiting_for(self.order, e, "buyer"), ("peer", "release_signature"))

    def test_waiting_for_an_old_seller_keeps_the_two_rounds(self):
        e = self._escrow(seller_leaf=False)
        self.assertEqual(flow.waiting_for(self.order, e, "buyer"), ("you", "release_signature"))


class KeysFlag(TestCase):
    def setUp(self):
        self.buyer, self.seller = _user(), _user()
        self.order = _order(self.buyer, self.seller, status=Order.Status.WF2)
        self.p = [patch.object(service, "coordinator_key", return_value=COORD_SECRET),
                  patch.object(service, "watch_funding")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_leaf_release_flag_is_stored_per_role(self):
        service.create_escrow(self.order)
        ks = derive_secret(b"s", "kilobot", self.order.id, "seller")
        fs = derive_secret(b"s", "kilobot", self.order.id, "funding")
        e = service.submit_keys(self.order, self.seller, pubkey(ks).hex(), pubkey(fs).hex(), leaf_release=True)
        self.assertTrue(e.funding_signatures.get("seller_leaf_release"))
        kb = derive_secret(b"b", "kilobot", self.order.id, "buyer")
        fb = derive_secret(b"b", "kilobot", self.order.id, "funding")
        e = service.submit_keys(self.order, self.buyer, pubkey(kb).hex(), pubkey(fb).hex())
        self.assertNotIn("buyer_leaf_release", e.funding_signatures)
        self.assertTrue(flow.seller_leaf(e))


class HybridPlan(TestCase):
    """Case A (buyer with a Lightning bond): rescue plan 1, the buyer signs nothing onchain."""

    def setUp(self):
        self.p = patch.object(service, "coordinator_key", return_value=COORD_SECRET)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_lightning_buyer_gets_rescue_plan_1(self):
        buyer, seller = _user(), _user()
        # SELL order: the maker sells, the taker (buyer) took with Lightning
        order = _order(buyer, seller, status=Order.Status.WF2, taker_lightning=True)
        order.type, order.maker, order.taker = Order.Types.SELL, seller, buyer
        order.save()
        e = service.create_escrow(order)
        self.assertEqual(service.rescue_plan(e), 1)
        self.assertEqual(e.funding_signatures.get("hybrid_plan"), service.HYBRID_PLAN)

    def test_lightning_seller_case_b_keeps_plan_2(self):
        buyer, seller = _user(), _user()
        # BUY order: the maker buys, the taker (seller) took with Lightning (case B)
        order = _order(buyer, seller, status=Order.Status.WF2, taker_lightning=True)
        e = service.create_escrow(order)
        self.assertEqual(service.rescue_plan(e), service.RESCUE_PLAN)
        self.assertNotIn("hybrid_plan", e.funding_signatures)

    def test_onchain_only_order_keeps_plan_2(self):
        buyer, seller = _user(), _user()
        order = _order(buyer, seller, status=Order.Status.WF2)
        e = service.create_escrow(order)
        self.assertEqual(service.rescue_plan(e), service.RESCUE_PLAN)
        self.assertNotIn("hybrid_plan", e.funding_signatures)

    def test_existing_escrows_keep_their_plan(self):
        buyer, seller = _user(), _user()
        order = _order(buyer, seller, status=Order.Status.WF2, taker_lightning=True)
        order.type, order.maker, order.taker = Order.Types.SELL, seller, buyer
        order.save()
        OnchainEscrow.objects.create(order=order, coordinator_pubkey=pubkey(COORD_SECRET).hex(), rescue_csv=2016,
                                     funding_signatures={"fee_plan": 2, "rescue_plan": 2, "extra_plan": 1})
        e = service.create_escrow(order)
        self.assertEqual(service.rescue_plan(e), 2)
