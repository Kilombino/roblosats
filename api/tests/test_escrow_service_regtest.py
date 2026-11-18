"""
Onchain escrow service against a regtest node with the BLAKE2b fork (opt-in).

Runs only with ESCROW_REGTEST=1 and BITCOIND_RPCURL pointing at a regtest Knots node whose
fork is active and that has a funded wallet called "faucet" (the lab in
/mnt/datos2/roblosats/lab-onchain). The traders' clients are played in Python with the same
keys a real client derives from its token.
"""

import math
import os
import uuid
from unittest import skipUnless

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.utils import timezone

from api.escrow import bip327 as M
from api.escrow import service
from api.escrow.taproot import derive_secret, pubkey, schnorr_sign, with_hashtype
from api.models import LNPayment, OnchainEscrow, Order

REGTEST = os.environ.get("ESCROW_REGTEST") == "1"


def _user():
    return User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")


def _order(buyer, seller):
    now = timezone.now()
    return Order.objects.create(
        type=Order.Types.BUY,
        status=Order.Status.WF2,
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


def _faucet(method, *params):
    return service.rpc(method, list(params), wallet="faucet")


def _mine(n=1):
    addr = _faucet("getnewaddress")
    while n > 0:  # in steps: one long generatetoaddress outlives the 30 s RPC timeout
        service.rpc("generatetoaddress", [min(n, 200), addr])
        n -= 200


def _spk(addr):
    return bytes.fromhex(_faucet("getaddressinfo", addr)["scriptPubKey"])


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowServiceRegtest(TransactionTestCase):
    def _trade(self, dispute):
        buyer, seller = _user(), _user()
        order = _order(buyer, seller)
        from unittest.mock import patch

        with patch.object(service, "RESCUE_PLAN", 1):  # low-level service test: no payout addresses
            escrow = service.create_escrow(order)
        alias = service.coordinator_alias()
        keys = {
            r: (derive_secret(f"tok-{r}-{order.id}".encode(), alias, order.id, r),
                derive_secret(f"tok-{r}-{order.id}".encode(), alias, order.id, "funding"))
            for r in ("seller", "buyer")
        }
        for r, user in (("seller", seller), ("buyer", buyer)):
            escrow = service.submit_keys(order, user, pubkey(keys[r][0]).hex(), pubkey(keys[r][1]).hex())
        self.assertTrue(escrow.address.startswith("bcrt1p"))

        seller_fee, buyer_fee = service.fee_split(order)
        for r, btc in (("seller", 0.0102), ("buyer", 0.00005)):
            addr = service.rpc("decodescript", [service.funding_spk(escrow, r).hex()])["address"]
            _faucet("sendtoaddress", addr, btc)
        _mine()

        coord_spk = _spk(_faucet("getnewaddress", "", "bech32m"))
        plan = service.build_deposit(escrow, 1_000_000, seller_fee, buyer_fee, coord_spk, 0, None)
        escrow.refresh_from_db()
        self.assertEqual(escrow.status, OnchainEscrow.Status.FUNDING)

        for r in ("seller", "buyer"):
            tx, spent, p = service.stored_deposit(escrow)
            sigs = [with_hashtype(schnorr_sign(keys[r][1], tx.sighash_unified(i, spent))).hex() for i in p[f"{r}_inputs"]]
            if sigs:
                escrow = service.submit_funding_signatures(escrow, r, sigs)
        escrow.refresh_from_db()
        self.assertEqual(escrow.status, OnchainEscrow.Status.MEMPOOL)
        _mine()
        escrow = service.refresh_confirmation(escrow)
        self.assertEqual(escrow.status, OnchainEscrow.Status.LOCKED)
        self.assertEqual(plan.tx.outputs[0].value, 1_000_000)

        buyer_spk = _spk(_faucet("getnewaddress", "", "bech32m"))
        if not dispute:
            service.start_release(escrow, buyer_spk, OnchainEscrow.ReleaseKind.COOPERATIVE)
            escrow.refresh_from_db()
            _, msg = service.release_message(escrow)
            c = service.contract_for(escrow)
            pks, tw, isx = c.musig_pubkeys(), [c.tweak], [True]
            secs = {}
            for i, r in enumerate(("seller", "buyer")):
                sec, pub = M.nonce_gen(keys[r][0], pks[i], c.output_key, msg, None)
                secs[r] = sec
                escrow = service.submit_release_nonce(escrow, r, pub.hex())
            agg = bytes.fromhex(service.aggregate_nonce(escrow))
            ctx = M.SessionContext(agg, pks, tw, isx, msg)
            for r in ("seller", "buyer"):
                escrow = service.submit_release_partial(escrow, r, M.sign(secs[r], keys[r][0], ctx).hex())
            self.assertEqual(escrow.status, OnchainEscrow.Status.RELEASED)
        else:
            service.start_release(escrow, buyer_spk, OnchainEscrow.ReleaseKind.BUYER_WINS)
            escrow.refresh_from_db()
            _, msg = service.release_message(escrow)
            with self.assertRaises(service.EscrowError):
                service.submit_leaf_signature(escrow, "seller", with_hashtype(schnorr_sign(keys["seller"][0], msg)).hex())
            escrow = service.submit_leaf_signature(escrow, "buyer", with_hashtype(schnorr_sign(keys["buyer"][0], msg)).hex())
            self.assertEqual(escrow.status, OnchainEscrow.Status.RELEASED)
        _mine()
        info = service.rpc("getrawtransaction", [escrow.release_txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        paid = [round(o["value"] * 1e8) for o in info["vout"] if o["scriptPubKey"]["hex"] == buyer_spk.hex()]
        self.assertEqual(len(paid), 1)
        self.assertGreater(paid[0], 1_000_000 - 2_000)

    def test_cooperative_release(self):
        self._trade(dispute=False)

    def test_dispute_release_to_buyer(self):
        self._trade(dispute=True)

    def test_keys_cannot_change_after_funding_starts(self):
        buyer, seller = _user(), _user()
        order = _order(buyer, seller)
        escrow = service.create_escrow(order)
        escrow.status = OnchainEscrow.Status.FUNDING
        escrow.save()
        k = pubkey(derive_secret(b"x", "a", 1, "seller")).hex()
        with self.assertRaises(service.EscrowError):
            service.submit_keys(order, seller, k, k)

    def test_outsider_cannot_submit_keys(self):
        buyer, seller = _user(), _user()
        order = _order(buyer, seller)
        service.create_escrow(order)
        k = pubkey(derive_secret(b"x", "a", 1, "seller")).hex()
        with self.assertRaises(service.EscrowError):
            service.submit_keys(order, _user(), k, k)




@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowOrderFlowRegtest(TransactionTestCase):
    """The whole order with onchain bonds and escrow, no Lightning at all:
    WFB → PUB → (take) → WF2 → WFE → CHA → FSE → PAY → SUC, a dispute solved from the admin,
    a collaborative cancellation and a taker that never funds (penalty broadcast)."""

    def setUp(self):
        from unittest.mock import patch

        self.patches = [patch("api.logics.send_notification"),
                        patch("api.logics.nostr_send_order_event"), patch("api.logics.send_devfund_donation"),
                        patch("api.models.order.send_notification")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _keys(self, order, r):
        # a fresh token per test: the lab node outlives the test database, order ids repeat
        if not hasattr(self, "salt"):
            self.salt = uuid.uuid4().hex
        alias = service.coordinator_alias()
        return {k: derive_secret(f"g-{r}-{order.id}-{self.salt}".encode(), alias, order.id, k) for k in ("escrow", "funding", "bond")}

    def _pay_bond(self, order, user, keys):
        from api.escrow import flow
        from api.escrow.bonds import penalty_message

        flow.action(order, user, "bond_key", {"bond_pubkey": pubkey(keys["bond"]).hex()})
        d = flow.order_data(order, user)
        _faucet("sendtoaddress", d["bond_address"], d["bond_required_sats"] / 1e8)
        _mine()
        flow.advance(order)
        bond = flow.bond_of(order, d["bond_role"], owner=user)
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.SIGN)
        self._sign_penalty(order, user, keys, bond)
        return bond

    def _sign_penalty(self, order, user, keys, bond):
        """The owner's penalty signature(s), one per coin of the bond (rl31-beta18)."""
        from api.escrow import flow
        from api.escrow.bonds import penalty_message

        _, msgs = penalty_message(bond)
        sigs = [with_hashtype(schnorr_sign(keys["bond"], m)).hex() for m in msgs]
        flow.action(order, user, "bond_penalty_signature", {"signature": sigs[0], "signatures": sigs})
        flow.advance(order)
        order.refresh_from_db()

    def _make_and_take(self):
        from api.escrow import flow
        from api.logics import Logics

        from api.models import Currency

        buyer, seller = _user(), _user()
        now = timezone.now()
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        order = Order.objects.create(**{**dict(
            currency=currency, type=Order.Types.BUY, status=Order.Status.WFB, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100,
            satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=buyer, bond_size=3, is_explicit=True,
        ), **self._order_fields(1_000_000)})
        kb, ks = self._keys(order, "buyer"), self._keys(order, "seller")
        self._pay_bond(order, buyer, kb)
        self.assertEqual(order.status, Order.Status.PUB)
        Logics.take(order, seller)
        self._pay_bond(order, seller, ks)
        self.assertEqual(order.status, Order.Status.WF2)
        self.assertIsNotNone(flow.escrow_of(order))
        return order, buyer, seller, {"buyer": kb, "seller": ks}

    def _to_chat(self):
        from api.escrow import flow
        from api.escrow.taproot import Transaction, TxOut

        order, buyer, seller, keys = self._make_and_take()
        for r, u in (("seller", seller), ("buyer", buyer)):
            flow.action(order, u, "escrow_keys", {"escrow_pubkey": pubkey(keys[r]["escrow"]).hex(), "funding_pubkey": pubkey(keys[r]["funding"]).hex()})
            flow.action(order, u, "escrow_address", {"address": _faucet("getnewaddress", "", "bech32m")})
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.WFE)
        for r, u in (("seller", seller), ("buyer", buyer)):
            d = flow.order_data(order, u)
            if "escrow_funding_address" not in d:  # beta9: the buyer's fee comes out of its bond
                self.assertTrue(d.get("escrow_fee_from_bond"))
                continue
            # rl31-beta20: a test may send another amount (test_escrow_funding_extra)
            _faucet("sendtoaddress", d["escrow_funding_address"], self._funding_amount(d, r) / 1e8)
        _mine()
        flow.advance(order)
        self.assertEqual(flow.escrow_of(order).status, OnchainEscrow.Status.FUNDING)
        self._sign_rescue(order, {"seller": seller, "buyer": buyer}, keys)
        for r, u in (("seller", seller), ("buyer", buyer)):
            d = flow.order_data(order, u)
            tx = Transaction.parse(bytes.fromhex(d["escrow_deposit_tx"]))
            spent = [TxOut(v, bytes.fromhex(sp)) for v, sp in d["escrow_deposit_spent"]]
            sigs = [with_hashtype(schnorr_sign(keys[r]["funding"], tx.sighash_unified(i, spent))).hex() for i in d["escrow_deposit_my_inputs"]]
            from api.escrow.bonds import contract_of
            bc = contract_of(flow.bond_of(order, d["bond_role"], owner=u))
            self.assertEqual(d["escrow_deposit_my_bond_inputs"][0], d["escrow_deposit_my_bond_input"])
            bsigs = [with_hashtype(schnorr_sign(keys[r]["bond"], tx.sighash_unified(bi, spent, 0x21, bc.leaf_hash(bc.COOPERATIVE)))).hex()
                     for bi in d["escrow_deposit_my_bond_inputs"]]
            payload = {"signatures": sigs, "bond_signature": bsigs[0]}
            if len(bsigs) > 1:
                payload["bond_signatures"] = bsigs
            flow.action(order, u, "escrow_funding_signatures", payload)
        _mine()
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CHA)
        escrow = flow.escrow_of(order)
        # beta9: the buyer's fee and mining share may have come out of its bond
        self.buyer_in = escrow.funding_signatures["plan"].get("bond_in_escrow", {}).get("buyer", 30_000)
        # rl31-beta15 (fee plan 2): both fees ride in the escrow output, no fee output in the deposit
        self.assertEqual(flow.fee_plan(escrow), 2)
        self.seller_fee, self.buyer_fee = flow.fees(order)
        self.assertGreaterEqual(self.seller_fee + self.buyer_fee, flow.min_coordinator_fee())
        self.assertEqual(flow.fees_held(escrow), {"seller": self.seller_fee, "buyer": self.buyer_fee})
        # rl31-beta20: funding excess too small for a change output rides in the escrow
        self.extra = flow._extras(escrow)
        self.assertEqual(escrow.escrow_sats, 1_000_000 + 30_000 + self.buyer_in + self.seller_fee + self.buyer_fee
                         + self.extra["seller"] + self.extra["buyer"])
        deposit = service.rpc("getrawtransaction", [escrow.funding_txid, True])
        coord = flow.coordinator_fee_spk().hex()
        self.assertFalse([o for o in deposit["vout"] if o["scriptPubKey"]["hex"] == coord])
        self.assertEqual(escrow.coordinator_fee_sats, 0)
        return order, buyer, seller, keys

    def _funding_amount(self, d, role):
        return d["escrow_funding_recommended"]

    def _order_fields(self, sats):
        """Extra Order fields (0.9.3: the 丰 sat subclasses)."""
        return {}

    def _deposit_rate(self):
        """Context around the advance that builds the deposit (0.9.3: a fee spike)."""
        import contextlib

        return contextlib.nullcontext()

    def _sign_rescue(self, order, users, keys):
        """rl31-beta16 (rescue plan 2): both sign the rescue before the deposit, as the client
        does, and each keeps the complete rescue (here: self.rescue_hex)."""
        from api.escrow import flow
        from api.escrow.taproot import Transaction, TxOut

        escrow = flow.escrow_of(order)
        if service.rescue_plan(escrow) < 2:
            return
        # the deposit signatures are refused until both rescue signatures are in
        with self.assertRaises(service.EscrowError):
            flow.action(order, users["seller"], "escrow_funding_signatures", {"signatures": []})
        for r in ("seller", "buyer"):
            d = flow.order_data(order, users[r])
            self.assertFalse(d["escrow_rescue_my_sig"])
            c = service.contract_for(escrow)
            tx = Transaction.parse(bytes.fromhex(d["escrow_rescue_tx"]))
            self.assertEqual(tx.inputs[0].txid, d["escrow_deposit_txid"])
            self.assertEqual(tx.inputs[0].sequence, escrow.rescue_csv)
            msg = tx.sighash_unified(0, [TxOut(escrow.escrow_sats, c.script_pubkey)], 0x21, c.leaf_hash(c.RESCUE))
            flow.action(order, users[r], "escrow_rescue_signature",
                        {"signature": with_hashtype(schnorr_sign(keys[r]["escrow"], msg)).hex()})
        escrow.refresh_from_db()
        self.rescue_hex = service.signed_rescue(escrow).serialize().hex()
        self.rescue_plan_data = escrow.funding_signatures["rescue"]

    def _release_outputs(self, escrow):
        """{'buyer'|'seller'|'coordinator'|other spk: sats} of the mined release, and its mining fee."""
        from api.escrow import flow

        _mine()
        escrow.refresh_from_db()
        info = service.rpc("getrawtransaction", [escrow.release_txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        names = {escrow.funding_signatures["buyer_payout_spk"]: "buyer",
                 escrow.funding_signatures["seller_payout_spk"]: "seller",
                 flow.coordinator_fee_spk().hex(): "coordinator"}
        outs = {}
        for o in info["vout"]:
            k = names.get(o["scriptPubKey"]["hex"], o["scriptPubKey"]["hex"])
            outs[k] = outs.get(k, 0) + round(o["value"] * 1e8)
        mining = escrow.escrow_sats - sum(outs.values())
        self.assertEqual(mining, math.ceil(math.ceil(info["vsize"]) * escrow.release_fee_rate))
        return outs, mining

    def _musig(self, order, users, keys):
        from api.escrow import flow

        escrow = flow.escrow_of(order)
        c = service.contract_for(escrow)
        _, msg = service.release_message(escrow)
        pks, tw, isx = c.musig_pubkeys(), [c.tweak], [True]
        secs = {}
        for i, r in enumerate(("seller", "buyer")):
            sec, pub = M.nonce_gen(keys[r]["escrow"], pks[i], c.output_key, msg, None)
            secs[r] = sec
            flow.action(order, users[r], "escrow_nonce", {"nonce": pub.hex()})
        ctx = M.SessionContext(bytes.fromhex(flow.order_data(order, users["buyer"])["escrow_release_aggnonce"]), pks, tw, isx, msg)
        for r in ("seller", "buyer"):
            flow.action(order, users[r], "escrow_partial", {"partial": M.sign(secs[r], keys[r]["escrow"], ctx).hex()})
        return flow.escrow_of(order)

    def test_happy_path_to_successful(self):
        from api.logics import Logics

        order, buyer, seller, keys = self._to_chat()
        self.assertEqual(Logics.confirm_fiat(order, buyer), (True, None))
        order.refresh_from_db()
        self.assertEqual(Logics.confirm_fiat(order, seller), (True, None))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAY)
        escrow = self._musig(order, {"seller": seller, "buyer": buyer}, keys)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)
        outs, mining = self._release_outputs(escrow)
        e = self.extra
        self.assertEqual(outs, {
            "buyer": 1_000_000 + self.buyer_in + e["buyer"] - mining,
            "seller": 30_000 + e["seller"],
            "coordinator": self.seller_fee + self.buyer_fee,
        })
        self.assertEqual(order.proceeds, self.seller_fee + self.buyer_fee)

    def _mined_outputs(self, escrow, txid):
        info = service.rpc("getrawtransaction", [txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        names = {escrow.funding_signatures["buyer_payout_spk"]: "buyer",
                 escrow.funding_signatures["seller_payout_spk"]: "seller"}
        outs = {}
        for o in info["vout"]:
            k = names.get(o["scriptPubKey"]["hex"], o["scriptPubKey"]["hex"])
            outs[k] = outs.get(k, 0) + round(o["value"] * 1e8)
        return outs

    def test_fair_rescue_without_the_coordinator(self):
        """rl31-beta16: the coordinator never answers again after the deposit. Before the CSV
        the pre-signed rescue is refused; after it, either trader broadcasts it straight to a
        node and each one gets its own: seller trade + its bond + its fee, buyer its bond + its
        fee (each paying half the mining fee)."""
        from api.escrow import flow

        order, buyer, seller, keys = self._to_chat()
        escrow = flow.escrow_of(order)
        self.assertEqual(service.rescue_plan(escrow), 2)
        self.assertEqual(escrow.rescue_csv, int(os.environ.get("ESCROW_RESCUE_CSV", 2016)))
        with self.assertRaises(service.EscrowError) as e:
            service.rpc("sendrawtransaction", [self.rescue_hex])
        self.assertIn("non-BIP68-final", str(e.exception))
        conf = service.rpc("getrawtransaction", [escrow.funding_txid, True])["confirmations"]
        _mine(escrow.rescue_csv - conf - 1)
        with self.assertRaises(service.EscrowError):  # one block short
            service.rpc("sendrawtransaction", [self.rescue_hex])
        _mine(1)
        txid = service.rpc("sendrawtransaction", [self.rescue_hex])
        _mine(1)
        outs = self._mined_outputs(escrow, txid)
        to_seller = 1_000_000 + 30_000 + self.seller_fee + self.extra["seller"]
        to_buyer = self.buyer_in + self.buyer_fee + self.extra["buyer"]
        self.assertEqual((self.rescue_plan_data["to_seller"], self.rescue_plan_data["to_buyer"]), (to_seller, to_buyer))
        fee = to_seller + to_buyer - outs["seller"] - outs["buyer"]
        self.assertEqual(outs, {"seller": to_seller - (fee - fee // 2), "buyer": to_buyer - fee // 2})
        self.assertLessEqual(fee, 5000)  # the client refuses more (MAX_RESCUE_FEE_SATS)

    def test_rescue_plan_1_kept_for_old_escrows(self):
        """An escrow created with the old rules (no rescue_plan): the seller alone, whole escrow."""
        from unittest.mock import patch

        from api.escrow import flow
        from api.escrow.taproot import Transaction, TxIn, TxOut, leaf_witness

        with patch.object(service, "RESCUE_PLAN", 1):
            order, buyer, seller, keys = self._to_chat()
        escrow = flow.escrow_of(order)
        self.assertEqual(service.rescue_plan(escrow), 1)
        self.assertNotIn("rescue", escrow.funding_signatures)
        c = service.contract_for(escrow)
        conf = service.rpc("getrawtransaction", [escrow.funding_txid, True])["confirmations"]
        _mine(escrow.rescue_csv - conf)
        dest = bytes.fromhex(escrow.funding_signatures["seller_payout_spk"])
        tx = Transaction([TxIn(escrow.funding_txid, escrow.funding_vout, escrow.rescue_csv)],
                         [TxOut(escrow.escrow_sats - 500, dest)])
        msg = tx.sighash_unified(0, [TxOut(escrow.escrow_sats, c.script_pubkey)], 0x21, c.leaf_hash(c.SELLER_RESCUE))
        tx.inputs[0].witness = leaf_witness(c, c.SELLER_RESCUE, [schnorr_sign(keys["seller"]["escrow"], msg)])
        txid = service.rpc("sendrawtransaction", [tx.serialize().hex()])
        _mine(1)
        self.assertEqual(self._mined_outputs(escrow, txid), {"seller": escrow.escrow_sats - 500})

    def test_collaborative_cancel(self):
        from api.escrow import flow
        from api.logics import Logics

        order, buyer, seller, keys = self._to_chat()
        Logics.cancel_order(order, buyer)
        order.refresh_from_db()
        Logics.cancel_order(order, seller)
        escrow = self._musig(order, {"seller": seller, "buyer": buyer}, keys)
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CCA)
        self.assertEqual(escrow.status, OnchainEscrow.Status.REFUNDED)
        outs, mining = self._release_outputs(escrow)
        # the coordinator fee goes back to each trader (Kilombino, 17 nov 2026)
        self.assertEqual(outs, {
            "seller": 1_000_000 + 30_000 + self.seller_fee + self.extra["seller"] - mining,
            "buyer": self.buyer_in + self.buyer_fee + self.extra["buyer"],
        })
        self.assertEqual(order.proceeds, 0)
        s = __import__("api.order_summary", fromlist=["x"]).final_summary(order, seller)
        self.assertEqual((s["coordinator_fee_sats"], s["fee_refunded_sats"]), (0, self.seller_fee))

    def _dispute(self):
        from unittest.mock import MagicMock
        from api.admin import OrderAdmin
        from api.logics import Logics
        from django.contrib.admin.sites import AdminSite

        order, buyer, seller, keys = self._to_chat()
        order.expires_at = timezone.now()
        order.save()
        self.assertEqual(Logics.open_dispute(order, buyer), (True, None))
        admin = OrderAdmin(Order, AdminSite())
        admin.message_user = MagicMock()
        return order, buyer, seller, keys, admin

    def test_dispute_for_the_buyer_from_the_admin(self):
        from unittest.mock import MagicMock, patch
        from api.escrow import flow

        order, buyer, seller, keys, admin = self._dispute()
        with patch("api.admin.send_notification"):
            admin.maker_wins(MagicMock(), Order.objects.filter(pk=order.pk))
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.status, OnchainEscrow.Status.RELEASING)
        _, msg = service.release_message(escrow)
        flow.action(order, buyer, "escrow_leaf_signature", {"signature": with_hashtype(schnorr_sign(keys["buyer"]["escrow"], msg)).hex()})
        outs, mining = self._release_outputs(escrow)
        # the winner gets what it got before beta15; the coordinator the fees and the penalty
        e = self.extra
        want = {
            "buyer": 1_000_000 + self.buyer_in + 15_000 + e["buyer"] - mining,
            "coordinator": 15_000 + self.seller_fee + self.buyer_fee,
        }
        # rl31-beta20: the loser's funding extra goes back to it (below dust: with the penalty)
        if e["seller"] >= 330:
            want["seller"] = e["seller"]
        else:
            want["coordinator"] += e["seller"]
        self.assertEqual(outs, want)

    def test_dispute_return_everything_from_the_admin(self):
        from unittest.mock import MagicMock, patch
        from api.escrow import flow

        order, buyer, seller, keys, admin = self._dispute()
        with patch("api.admin.send_notification"):
            admin.return_everything(MagicMock(), Order.objects.filter(pk=order.pk))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CCA)
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.status, OnchainEscrow.Status.RELEASING)
        self.assertEqual(escrow.release_kind, OnchainEscrow.ReleaseKind.SELLER_WINS)
        _, msg = service.release_message(escrow)
        flow.action(order, seller, "escrow_leaf_signature", {"signature": with_hashtype(schnorr_sign(keys["seller"]["escrow"], msg)).hex()})
        outs, mining = self._release_outputs(escrow)
        # like a collaborative cancel: nothing for the coordinator
        self.assertEqual(outs, {
            "seller": 1_000_000 + 30_000 + self.seller_fee + self.extra["seller"] - mining,
            "buyer": self.buyer_in + self.buyer_fee + self.extra["buyer"],
        })
        self.assertEqual(flow.proceeds(order), 0)

    def test_taker_never_funds_is_penalized(self):
        from api.escrow import flow
        from api.logics import Logics

        order, buyer, seller, keys = self._make_and_take()
        for r, u in (("seller", seller), ("buyer", buyer)):
            flow.action(order, u, "escrow_keys", {"escrow_pubkey": pubkey(keys[r]["escrow"]).hex(), "funding_pubkey": pubkey(keys[r]["funding"]).hex()})
            flow.action(order, u, "escrow_address", {"address": _faucet("getnewaddress", "", "bech32m")})
        flow.advance(order)
        order.refresh_from_db()
        d = flow.order_data(order, buyer)
        if "escrow_funding_address" in d:  # beta9: the buyer's fee may come out of its bond
            _faucet("sendtoaddress", d["escrow_funding_address"], d["escrow_funding_recommended"] / 1e8)
        _mine()
        order.expires_at = timezone.now()
        order.save()
        Logics.order_expires(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.EXP)
        seller_bond = flow.bond_of(order, "taker")
        self.assertEqual(seller_bond.status, seller_bond.Status.PENALIZED)
        self.assertEqual(flow.bond_of(order, "maker").status, seller_bond.Status.RETURNABLE)
        _mine()
        self.assertGreaterEqual(service.rpc("getrawtransaction", [seller_bond.spend_txid, True])["confirmations"], 1)


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowHybridRegtest(EscrowOrderFlowRegtest):
    """rl31-beta10: a Lightning taker on an onchain order (the Lightning side mocked, the
    onchain side against the node). A: the buyer takes a SELL with a Lightning bond; B: the
    seller takes a BUY with Lightning, the order runs over Lightning and the maker's onchain
    bond sits behind a proxy LNPayment."""

    # only the helpers of the parent class, not its tests again
    test_happy_path_to_successful = None
    test_collaborative_cancel = None
    test_dispute_for_the_buyer_from_the_admin = None
    test_dispute_return_everything_from_the_admin = None
    test_taker_never_funds_is_penalized = None

    def _ln_take(self, order, taker):
        import uuid as _uuid

        from api.logics import Logics
        from api.models import TakeOrder

        Logics.take(order, taker, lightning=True)
        take = TakeOrder.objects.get(order=order, taker=taker)
        now = timezone.now()
        take.taker_bond = LNPayment.objects.create(
            concept=LNPayment.Concepts.TAKEBOND, type=LNPayment.Types.HOLD, sender=taker,
            status=LNPayment.Status.LOCKED, num_satoshis=30_000, payment_hash=_uuid.uuid4().hex,
            preimage=_uuid.uuid4().hex, invoice=f"lnbcrt{_uuid.uuid4().hex}", created_at=now,
            expires_at=now + timezone.timedelta(hours=3),
        )
        take.save(update_fields=["taker_bond"])
        Logics.finalize_contract(take)
        order.refresh_from_db()
        return order

    def _make(self, maker_sells, sats=1_000_000):
        from api.models import Currency

        maker, taker = _user(), _user()
        User.objects.get_or_create(username="admin")
        now = timezone.now()
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        order = Order.objects.create(**{**dict(
            currency=currency, type=Order.Types.SELL if maker_sells else Order.Types.BUY, status=Order.Status.WFB,
            escrow_type=Order.EscrowTypes.ONCHAIN, created_at=now, expires_at=now + timezone.timedelta(hours=24),
            has_range=False, amount=100, satoshis=sats, t0_satoshis=sats, last_satoshis=sats,
            maker=maker, bond_size=3 if sats >= 1_000_000 else 30, is_explicit=True,
        ), **self._order_fields(sats)})
        km = self._keys(order, "seller" if maker_sells else "buyer")
        self._pay_bond(order, maker, km)
        self.assertEqual(order.status, Order.Status.PUB)
        return order, maker, taker, km

    def _case_a(self, T):
        from unittest.mock import patch

        from api.escrow import flow
        from api.escrow.bonds import contract_of
        from api.escrow.taproot import Transaction, TxOut
        from api.logics import Logics

        order, seller, buyer, ks = self._make(maker_sells=True, sats=T)
        order = self._ln_take(order, buyer)
        self.assertEqual(order.escrow_type, Order.EscrowTypes.ONCHAIN)
        self.assertTrue(order.taker_lightning)
        keys = {"seller": ks, "buyer": self._keys(order, "buyer")}
        users = {"seller": seller, "buyer": buyer}
        for r, u in users.items():
            flow.action(order, u, "escrow_keys", {"escrow_pubkey": pubkey(keys[r]["escrow"]).hex(), "funding_pubkey": pubkey(keys[r]["funding"]).hex()})
            flow.action(order, u, "escrow_address", {"address": _faucet("getnewaddress", "", "bech32m")})
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.WFE)
        db = flow.order_data(order, buyer)
        self.assertTrue(db["escrow_fee_at_release"])
        self.assertNotIn("escrow_funding_address", db)
        self.assertEqual(flow.waiting_for(order, flow.escrow_of(order), "buyer"), ("peer", "funding"))
        ds = flow.order_data(order, seller)
        _faucet("sendtoaddress", ds["escrow_funding_address"], ds["escrow_funding_recommended"] / 1e8)
        _mine()
        self.assertEqual(flow.order_data(order, seller).get("escrow_funding_confirmed_sats"), ds["escrow_funding_recommended"])
        with self._deposit_rate():
            flow.advance(order)
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.status, OnchainEscrow.Status.FUNDING)
        plan = escrow.funding_signatures["plan"]
        self.assertEqual(list(plan["bond_ids"]), ["seller"])
        self.assertEqual(plan["buyer_inputs"], [])
        seller_fee, buyer_fee = flow.fees(order)
        self.case_a_fees = (seller_fee, buyer_fee)
        self.assertEqual(plan["buyer_fee_at_release"], buyer_fee)
        self._sign_rescue(order, {"seller": seller, "buyer": buyer}, keys)
        self.assertEqual(self.rescue_plan_data["to_buyer"], 0)  # Lightning bond: nothing inside
        self.assertEqual(len(Transaction.parse(bytes.fromhex(self.rescue_hex)).outputs), 1)
        for r, u in (("buyer", buyer), ("seller", seller)):  # the buyer's app sends no signature
            d = flow.order_data(order, u)
            tx = Transaction.parse(bytes.fromhex(d["escrow_deposit_tx"]))
            spent = [TxOut(v, bytes.fromhex(sp)) for v, sp in d["escrow_deposit_spent"]]
            sigs = [with_hashtype(schnorr_sign(keys[r]["funding"], tx.sighash_unified(i, spent))).hex() for i in d["escrow_deposit_my_inputs"]]
            bsig = None
            if d["escrow_deposit_my_bond_input"] is not None:
                bc = contract_of(flow.bond_of(order, d["bond_role"], owner=u))
                bsig = with_hashtype(schnorr_sign(keys[r]["bond"], tx.sighash_unified(d["escrow_deposit_my_bond_input"], spent, 0x21, bc.leaf_hash(bc.COOPERATIVE)))).hex()
            flow.action(order, u, "escrow_funding_signatures", {"signatures": sigs, "bond_signature": bsig})
        _mine()
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CHA)
        # fee plan 2: the seller's fee rides in the escrow, no fee output in the deposit
        self.assertEqual(flow.escrow_of(order).escrow_sats, T + 30_000 + seller_fee)
        self.assertGreaterEqual(seller_fee + buyer_fee, flow.min_coordinator_fee())
        self.assertEqual(Logics.confirm_fiat(order, buyer), (True, None))
        order.refresh_from_db()
        with patch("api.logics.LNNode") as ln:
            self.assertEqual(Logics.confirm_fiat(order, seller), (True, None))
            ln.cancel_return_hold_invoice.assert_called_once_with(order.taker_bond.payment_hash)
        order.refresh_from_db()
        self.assertEqual(order.taker_bond.status, LNPayment.Status.RETNED)
        escrow = self._musig(order, users, keys)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)
        _mine()
        info = service.rpc("getrawtransaction", [escrow.release_txid, True])
        outs = {o["scriptPubKey"]["address"]: round(o["value"] * 1e8) for o in info["vout"]}
        mining = math.ceil(math.ceil(info["vsize"]) * escrow.release_fee_rate)
        values = sorted(outs.values())
        # one coordinator output for the whole order (the 1,000-sat minimum in total)
        self.assertEqual(values, sorted([seller_fee + buyer_fee, 30_000, T - buyer_fee - mining]))
        self.assertEqual(outs[service.rpc("decodescript", [flow.coordinator_fee_spk().hex()])["address"]], seller_fee + buyer_fee)
        self.assertEqual(order.proceeds, seller_fee + buyer_fee)
        summary = flow.trade_summary(order, buyer)
        self.assertTrue(summary["lightning_bond"])
        self.assertTrue(summary["fee_at_release"])
        self.assertEqual(summary["received_sats"], T - buyer_fee - mining)

    def test_case_a_lightning_buyer_to_successful(self):
        self._case_a(1_000_000)

    def test_case_a_small_order_one_minimum_in_total(self):
        """rl31-beta15: 100,000 sats at 0.19 % = 190 sats, raised to 1,000 IN TOTAL (not 1,000
        per output as in beta13/14)."""
        from api.escrow import flow

        self._case_a(100_000)
        self.assertEqual(self.case_a_fees[0] + self.case_a_fees[1], flow.min_coordinator_fee())

    def test_case_b_proxy_return_and_penalty_onchain(self):
        from unittest.mock import patch

        from api.escrow import bonds, flow
        from api.escrow.taproot import SIGHASH_ALL_UNIFIED, Transaction, TxOut
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, seller, kb = self._make(maker_sells=False)
        order = self._ln_take(order, seller)
        self.assertEqual(order.escrow_type, Order.EscrowTypes.LIGHTNING)
        self.assertTrue(flow.has_proxy(order))
        self.assertIsNone(flow.escrow_of(order))
        # the trade goes well: the proxy is returned when the seller confirms -> onchain bond free
        with patch("api.logics.LNNode") as ln:
            Logics.return_bond(order.maker_bond)
            ln.cancel_return_hold_invoice.assert_not_called()
        bond = flow.bond_of(order, "maker")
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)
        back = _faucet("getnewaddress", "", "bech32m")
        flow.action(order, buyer, "bond_return", {"address": back})
        bond.refresh_from_db()
        c = bonds.contract_of(bond)
        tx = Transaction.parse(bytes.fromhex(bond.return_tx))
        msg = tx.sighash_unified(0, [TxOut(bond.value, c.script_pubkey)], SIGHASH_ALL_UNIFIED, c.leaf_hash(c.COOPERATIVE))
        flow.action(order, buyer, "bond_return_signature", {"signature": with_hashtype(schnorr_sign(kb["bond"], msg)).hex()})
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNED)
        _mine()
        info = service.rpc("getrawtransaction", [bond.spend_txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        self.assertEqual(info["vout"][0]["scriptPubKey"]["address"], back)

        # another one where the maker (buyer) lets it expire: settling the proxy = onchain penalty
        order, buyer, seller, kb = self._make(maker_sells=False)
        order = self._ln_take(order, seller)
        self.assertTrue(Logics.settle_bond(order.maker_bond))
        self.assertFalse(Logics.settle_bond(order.maker_bond))
        bond = flow.bond_of(order, "maker")
        self.assertEqual(bond.status, OnchainBond.Status.PENALIZED)
        _mine()
        self.assertGreaterEqual(service.rpc("getrawtransaction", [bond.spend_txid, True])["confirmations"], 1)
        self.assertIsNone(Logics.reward_source_problem(order, order.maker_bond, order.taker_bond, 15_000))


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowSatOrderRegtest(EscrowHybridRegtest):
    """0.9.3 (Kilobot #24): a "丰 sat" order (sats_denominated, paid in Spamcoin at a fixed
    price) taken onchain and with Lightning, and a fee spike between the recommended funding
    and the deposit."""

    test_happy_path_to_successful = EscrowOrderFlowRegtest.test_happy_path_to_successful
    test_case_b_proxy_return_and_penalty_onchain = None
    test_case_a_small_order_one_minimum_in_total = None

    def _order_fields(self, sats):
        from decimal import Decimal

        from api.models import Currency

        spam, _ = Currency.objects.get_or_create(currency=1000, defaults={"exchange_rate": 0.0034})
        return {"currency": spam, "sats_denominated": True, "amount": sats, "fixed_price": Decimal("0.0021"),
                "is_explicit": False, "satoshis": None, "premium": 0}

    def test_onchain_sat_order_keeps_its_amounts(self):
        from api.logics import Logics

        order, buyer, seller, keys = self._make_and_take()
        self.assertTrue(order.sats_denominated)
        self.assertEqual(order.last_satoshis, 1_000_000)
        self.assertAlmostEqual(Logics.exchange_total(order, 1_000_000), 0.000021)


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowFeeSpikeRegtest(EscrowSatOrderRegtest):
    """The fee rate triples between the recommended funding (frozen when shown) and the
    deposit: the seller paid what it was told and the deposit still gets built (#24)."""

    test_happy_path_to_successful = None
    test_onchain_sat_order_keeps_its_amounts = None

    def _deposit_rate(self):
        from unittest.mock import patch

        rate = service.fee_rate(2)
        return patch("api.escrow.service.fee_rate", return_value=rate * 3)
