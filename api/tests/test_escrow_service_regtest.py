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
from api.escrow.taproot import Transaction, derive_secret, pubkey, schnorr_sign, with_hashtype
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
        # the whole escrow less the release's mining fee at its rate (0.9.7: the lab's fee rate
        # grew past the old fixed 2,000-sat margin)
        escrow.refresh_from_db()
        self.assertEqual(paid[0], 1_000_000 - math.ceil(math.ceil(info["vsize"]) * escrow.release_fee_rate))

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

        payload = {"bond_pubkey": pubkey(keys["bond"]).hex()}
        ret = self._return_address(user)
        if ret:
            payload["return_address"] = ret
        flow.action(order, user, "bond_key", payload)
        d = flow.order_data(order, user)
        _faucet("sendtoaddress", d["bond_address"], d["bond_required_sats"] / 1e8)
        _mine()
        flow.advance(order)
        bond = flow.bond_of(order, d["bond_role"], owner=user)
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.SIGN)
        self._sign_penalty(order, user, keys, bond)
        if self.SIGN_AUTO_RETURN:
            self._sign_auto_return(order, user, keys)
        return bond

    # 0.9.7: automatic return of the bond (the app gives a return address and pre-signs)
    AUTO_RETURN = False
    SIGN_AUTO_RETURN = False

    def _return_address(self, user):
        if not self.AUTO_RETURN:
            return None
        if not hasattr(self, "return_addresses"):
            self.return_addresses = {}
        self.return_addresses.setdefault(user.id, _faucet("getnewaddress", "", "bech32m"))
        return self.return_addresses[user.id]

    def _sign_auto_return(self, order, user, keys):
        from api.escrow import bonds, flow

        d = flow.order_data(order, user)
        if not d.get("bond_auto_return_tx"):
            return
        bond = flow.bond_of(order, d["bond_role"], owner=user)
        _, msgs = bonds.auto_return_message(bond)
        sigs = [with_hashtype(schnorr_sign(keys["bond"], m)).hex() for m in msgs]
        flow.action(order, user, "bond_auto_return_signature", {"signatures": sigs})
        self.assertTrue(flow.order_data(order, user)["bond_auto_return_signed"])

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
            flow.action(order, u, "escrow_keys", self._keys_payload(keys, r))
            flow.action(order, u, "escrow_address", {"address": self._payout_address(r)})
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

    # 0.9.5: the seller's app announces with its keys that it signs the cooperative release
    # alone through its leaf; False plays an app up to 0.9.4 (two-round MuSig2 release).
    LEAF = False

    # 0.9.9: the app accepts the deposit change at its payout address; the payout address type
    CHANGE_TO_PAYOUT = False

    def _payout_address(self, role):
        return _faucet("getnewaddress", "", "bech32m")

    def _keys_payload(self, keys, r):
        payload = {"escrow_pubkey": pubkey(keys[r]["escrow"]).hex(), "funding_pubkey": pubkey(keys[r]["funding"]).hex()}
        if self.LEAF:
            payload["leaf_release"] = True
        if self.CHANGE_TO_PAYOUT:
            payload["change_to_payout"] = True
        return payload

    def _leaf(self, order, users, keys):
        """0.9.5: the seller alone signs the release on leaf 0; the buyer signs nothing."""
        from api.escrow import flow

        escrow = flow.escrow_of(order)
        self.assertTrue(flow.order_data(order, users["seller"])["escrow_release_leaf_ok"])
        self.assertFalse(flow.order_data(order, users["buyer"])["escrow_release_leaf_ok"])
        _, msg = service.cooperative_leaf_message(escrow)
        flow.action(order, users["seller"], "escrow_release_leaf",
                    {"signature": with_hashtype(schnorr_sign(keys["seller"]["escrow"], msg)).hex()})
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.funding_signatures["release_path"], "seller_leaf")
        self.assertEqual(escrow.release_partial_sigs, {})
        return escrow

    def _release(self, order, users, keys):
        if self.LEAF:
            escrow = self._leaf(order, users, keys)
            self.assertEqual(escrow.release_nonces, {})
            return escrow
        return self._musig(order, users, keys)

    def test_happy_path_to_successful(self):
        from api.logics import Logics

        order, buyer, seller, keys = self._to_chat()
        self.assertEqual(Logics.confirm_fiat(order, buyer), (True, None))
        order.refresh_from_db()
        self.assertEqual(Logics.confirm_fiat(order, seller), (True, None))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAY)
        escrow = self._release(order, {"seller": seller, "buyer": buyer}, keys)
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
        escrow = self._release(order, {"seller": seller, "buyer": buyer}, keys)
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
            flow.action(order, u, "escrow_keys", self._keys_payload(keys, r))
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
        if service.rescue_plan(escrow) >= 2:  # escrows up to 0.9.4 (no hybrid_plan)
            self.assertNotIn("hybrid_plan", escrow.funding_signatures)
            self.assertEqual(self.rescue_plan_data["to_buyer"], 0)  # Lightning bond: nothing inside
            self.assertEqual(len(Transaction.parse(bytes.fromhex(self.rescue_hex)).outputs), 1)
        else:
            # 0.9.5 (hybrid_plan 1): the buyer has nothing inside, the seller's own rescue leaf
            self.assertEqual(escrow.funding_signatures["hybrid_plan"], 1)
            self.assertNotIn("rescue", escrow.funding_signatures)
            self.assertEqual(flow.waiting_for(order, escrow, "buyer"), ("peer", "deposit_signature"))
        hybrid = escrow.funding_signatures.get("hybrid_plan")
        for r, u in (("buyer", buyer), ("seller", seller)):  # the buyer's app sends no signature
            if r == "buyer" and hybrid:
                continue  # 0.9.5: the buyer does not even open the order for the deposit
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
        escrow = self._release(order, users, keys)
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
        if hybrid and self.LEAF:
            # 0.9.5 mixed A: the buyer acted only at the take (keys, address) and "fiat sent"
            fs = escrow.funding_signatures
            self.assertFalse([k for k in fs if k.startswith("buyer_") and ("sig" in k)])
            self.assertEqual(escrow.release_nonces, {})
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


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowLeafFlowRegtest(EscrowOrderFlowRegtest):
    """0.9.5: the whole onchain order with apps that sign the cooperative release alone (seller
    leaf with the coordinator): happy path, collaborative cancel, disputes, fair rescue."""

    LEAF = True


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowMidMusigRegtest(EscrowOrderFlowRegtest):
    """Order #24: a release started with the two-round signing (app up to 0.9.4, release planned
    for the key path), the seller's nonce in, the buyer gone. The seller's 0.9.5 app finishes it
    through its leaf: same outputs, confirmed."""

    test_happy_path_to_successful = None
    test_fair_rescue_without_the_coordinator = None
    test_rescue_plan_1_kept_for_old_escrows = None
    test_collaborative_cancel = None
    test_dispute_for_the_buyer_from_the_admin = None
    test_dispute_return_everything_from_the_admin = None
    test_taker_never_funds_is_penalized = None

    def test_leaf_finishes_a_release_started_with_musig(self):
        from api.escrow import flow
        from api.escrow.taproot import Transaction
        from api.logics import Logics

        order, buyer, seller, keys = self._to_chat()
        self.assertFalse(flow.seller_leaf(flow.escrow_of(order)))  # old app: no flag
        Logics.confirm_fiat(order, buyer)
        order.refresh_from_db()
        Logics.confirm_fiat(order, seller)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAY)
        escrow = flow.escrow_of(order)
        c = service.contract_for(escrow)
        _, msg = service.release_message(escrow)
        _, pub = M.nonce_gen(keys["seller"]["escrow"], c.musig_pubkeys()[0], c.output_key, msg, None)
        flow.action(order, seller, "escrow_nonce", {"nonce": pub.hex()})
        escrow = flow.escrow_of(order)
        self.assertEqual(flow.waiting_for(order, escrow, "seller"), ("peer", "release_signature"))
        planned = [(o.value, o.script_pubkey) for o in Transaction.parse(bytes.fromhex(escrow.release_tx)).outputs]
        escrow = self._leaf(order, {"seller": seller, "buyer": buyer}, keys)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)
        _mine()
        info = service.rpc("getrawtransaction", [escrow.release_txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        mined = [(round(o["value"] * 1e8), bytes.fromhex(o["scriptPubKey"]["hex"])) for o in info["vout"]]
        self.assertEqual(mined, planned)
        fee = escrow.escrow_sats - sum(v for v, _ in mined)
        self.assertGreaterEqual(fee / info["vsize"], service.MIN_RELEASE_RATE)
        self.assertEqual(len(info["vin"][0]["txinwitness"]), 4)  # leaf spend, not key path


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowHybridLeafRegtest(EscrowHybridRegtest):
    """0.9.5 mixed A: hybrid_plan 1 (seller rescue leaf) and the seller-leaf release: the buyer
    acts only at the take and at "fiat sent"; the seller at publish, funding and "received"."""

    LEAF = True
    test_case_b_proxy_return_and_penalty_onchain = None


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowHybridOldPlanRegtest(EscrowHybridRegtest):
    """Mixed A escrows created before 0.9.5 (no hybrid_plan: fair rescue both pre-sign) with
    apps up to 0.9.4 (two-round release): still work."""

    test_case_b_proxy_return_and_penalty_onchain = None

    def setUp(self):
        from unittest.mock import patch

        super().setUp()
        self.old_plan = patch.object(service, "HYBRID_PLAN", 0)
        self.old_plan.start()

    def tearDown(self):
        self.old_plan.stop()
        super().tearDown()


def _paid_to(txid, address):
    info = service.rpc("getrawtransaction", [txid, True])
    return info.get("confirmations", 0), sum(round(o["value"] * 1e8) for o in info["vout"] if o["scriptPubKey"].get("address") == address)


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowAutoReturnRegtest(EscrowOrderFlowRegtest):
    """0.9.7: the bond goes back by itself to the return address once it is free (pre-signed
    with the penalty, broadcast by the coordinator from release())."""

    AUTO_RETURN = True
    SIGN_AUTO_RETURN = True
    test_happy_path_to_successful = None
    test_fair_rescue_without_the_coordinator = None
    test_rescue_plan_1_kept_for_old_escrows = None
    test_collaborative_cancel = None
    test_dispute_for_the_buyer_from_the_admin = None
    test_dispute_return_everything_from_the_admin = None
    test_taker_never_funds_is_penalized = None

    def _make_public(self):
        from api.models import Currency

        buyer = _user()
        now = timezone.now()
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        order = Order.objects.create(
            currency=currency, type=Order.Types.BUY, status=Order.Status.WFB, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100,
            satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=buyer, bond_size=3, is_explicit=True,
        )
        kb = self._keys(order, "buyer")
        bond = self._pay_bond(order, buyer, kb)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PUB)
        return order, buyer, kb, bond

    def test_cancelled_public_order_returns_the_bond_by_itself(self):
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, kb, bond = self._make_public()
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.LOCKED)  # held: nothing broadcast
        Logics.cancel_order(order, buyer)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNED)
        _mine()
        conf, paid = _paid_to(bond.spend_txid, self.return_addresses[buyer.id])
        self.assertGreaterEqual(conf, 1)
        self.assertGreater(paid, bond.value - 3_000)  # the whole bond less the mining fee

    def test_taker_bond_freed_when_the_maker_cancels(self):
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, seller, keys = self._make_and_take()
        maker_bond = flow_bond(order, "maker")
        taker_bond = flow_bond(order, "taker")
        Logics.cancel_order(order, buyer)  # WF2: maker penalized, taker bond free
        maker_bond.refresh_from_db()
        taker_bond.refresh_from_db()
        self.assertEqual(maker_bond.status, OnchainBond.Status.PENALIZED)
        self.assertEqual(taker_bond.status, OnchainBond.Status.RETURNED)
        _mine()
        conf, paid = _paid_to(taker_bond.spend_txid, self.return_addresses[seller.id])
        self.assertGreaterEqual(conf, 1)
        self.assertGreater(paid, 0)

    def test_bond_in_a_deposit_is_never_returned_again(self):
        """The bond goes into the deposit and comes back inside the release, once."""
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, seller, keys = self._to_chat()
        for role in ("maker", "taker"):
            b = flow_bond(order, role)
            self.assertEqual(b.status, OnchainBond.Status.IN_DEPOSIT)
            self.assertTrue((b.auto_return or {}).get("sigs"))
        self.assertEqual(Logics.confirm_fiat(order, buyer), (True, None))
        order.refresh_from_db()
        self.assertEqual(Logics.confirm_fiat(order, seller), (True, None))
        order.refresh_from_db()
        self._musig(order, {"seller": seller, "buyer": buyer}, keys)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)
        for role in ("maker", "taker"):
            b = flow_bond(order, role)
            b.refresh_from_db()
            self.assertEqual(b.status, OnchainBond.Status.IN_DEPOSIT)  # never RETURNED by itself
            self.assertFalse(flow_bond_release(order, role))  # releasing again does nothing


def flow_bond(order, role):
    from api.escrow import flow

    return flow.bond_of(order, role)


def flow_bond_release(order, role):
    from api.escrow import bonds, flow

    return bonds.release(flow.bond_of(order, role))


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowAutoReturnFallbackRegtest(EscrowAutoReturnRegtest):
    """0.9.7: without the pre-signed return (an old app) or with one that no longer fits, the
    bond stays free for the manual "Get my bond back"."""

    SIGN_AUTO_RETURN = False
    test_cancelled_public_order_returns_the_bond_by_itself = None
    test_taker_bond_freed_when_the_maker_cancels = None
    test_bond_in_a_deposit_is_never_returned_again = None

    def _manual_return(self, order, user, kb, bond):
        from api.escrow import bonds, flow
        from api.models import OnchainBond

        back = _faucet("getnewaddress", "", "bech32m")
        flow.action(order, user, "bond_return", {"address": back})
        bond.refresh_from_db()
        tx = Transaction.parse(bytes.fromhex(bond.return_tx))
        msgs = bonds._coin_messages(bond, tx, bonds.return_coins_of(bond))
        flow.action(order, user, "bond_return_signature",
                    {"signatures": [with_hashtype(schnorr_sign(kb["bond"], m)).hex() for m in msgs]})
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNED)
        _mine()
        self.assertGreaterEqual(_paid_to(bond.spend_txid, back)[0], 1)

    def test_unsigned_return_falls_back_to_manual(self):
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, kb, bond = self._make_public()
        bond.refresh_from_db()
        self.assertFalse((bond.auto_return or {}).get("sigs"))
        Logics.cancel_order(order, buyer)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)
        self._manual_return(order, buyer, kb, bond)

    def test_return_that_no_longer_fits_falls_back_to_manual(self):
        """Signed, but for other coins (e.g. a bond whose coins changed): not sent."""
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, kb, bond = self._make_public()
        self._sign_auto_return(order, buyer, kb)
        bond.refresh_from_db()
        ar = dict(bond.auto_return)
        ar["coins"] = [[c[0], c[1], c[2] + 1] for c in ar["coins"]]  # another amount
        bond.auto_return = ar
        bond.save(update_fields=["auto_return"])
        Logics.cancel_order(order, buyer)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)
        self._manual_return(order, buyer, kb, bond)


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowHybridAutoReturnRegtest(EscrowHybridRegtest):
    """0.9.7, hybrid B: the buyer's onchain maker bond goes back by itself after the trade."""

    AUTO_RETURN = True
    SIGN_AUTO_RETURN = True
    test_case_a_lightning_buyer_to_successful = None
    test_case_a_small_order_one_minimum_in_total = None
    test_case_b_proxy_return_and_penalty_onchain = None

    def test_hybrid_b_success_returns_the_maker_bond_by_itself(self):
        from unittest.mock import patch

        from api.escrow import flow
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, seller, kb = self._make(maker_sells=False)
        order = self._ln_take(order, seller)
        self.assertTrue(flow.has_proxy(order))
        bond = flow.bond_of(order, "maker")
        self.assertEqual(bond.status, OnchainBond.Status.LOCKED)
        with patch("api.logics.LNNode"):
            Logics.return_bond(order.maker_bond)  # the trade went well
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNED)
        _mine()
        conf, paid = _paid_to(bond.spend_txid, self.return_addresses[buyer.id])
        self.assertGreaterEqual(conf, 1)
        self.assertGreater(paid, bond.value - 3_000)


class EscrowTakeRaceRegtest(EscrowHybridRegtest):
    """0.9.8: an onchain taker waits for the confirmation of its bond while another taker locks
    first (a Lightning bond locks in seconds). The loser's bond is freed at once, never
    penalized; its robot is told, and the order page shows it only its bond, to take it back.
    Before 0.9.8 the bond stayed "waiting" (case B: forever) and the page answered 403."""

    test_case_a_lightning_buyer_to_successful = None
    test_case_a_small_order_one_minimum_in_total = None
    test_case_b_proxy_return_and_penalty_onchain = None

    def setUp(self):
        from unittest.mock import patch

        super().setUp()
        self.notify = patch("api.tasks.send_notification").start()
        self.patches.append(self.notify)

    def tearDown(self):
        from unittest.mock import patch

        patch.stopall()

    def _call(self, method, order, user, body=None):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from api.views import OrderView

        f = APIRequestFactory()
        if method == "get":
            req = f.get(f"/api/order/?order_id={order.id}")
            view = OrderView.as_view({"get": "get"})
        else:
            req = f.post(f"/api/order/?order_id={order.id}", body, format="json")
            view = OrderView.as_view({"post": "take_update_confirm_dispute_cancel"})
        force_authenticate(req, user=user)
        return view(req)

    def _onchain_take(self, order, confirm=False):
        """A taker that sends its onchain bond (left in the mempool unless confirm)."""
        from api.escrow import flow
        from api.logics import Logics

        loser = _user()
        Logics.take(order, loser)
        keys = self._keys(order, f"loser-{loser.id}")
        flow.action(order, loser, "bond_key", {"bond_pubkey": pubkey(keys["bond"]).hex()})
        d = flow.order_data(order, loser)
        _faucet("sendtoaddress", d["bond_address"], d["bond_required_sats"] / 1e8)
        if confirm:
            _mine()
        flow.advance(order)
        return loser, keys, flow.bond_of(order, "taker", owner=loser)

    def _get_bond_back(self, order, loser, keys, bond):
        from api.escrow import bonds
        from api.models import OnchainBond

        back = _faucet("getnewaddress", "", "bech32m")
        r = self._call("post", order, loser, {"action": "bond_return", "escrow": {"address": back}})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["lost_take"])
        self.assertTrue(r.data["bond_return_tx"])
        bond.refresh_from_db()
        tx = Transaction.parse(bytes.fromhex(bond.return_tx))
        msgs = bonds._coin_messages(bond, tx, bonds.return_coins_of(bond))
        sigs = [with_hashtype(schnorr_sign(keys["bond"], m)).hex() for m in msgs]
        r = self._call("post", order, loser, {"action": "bond_return_signature", "escrow": {"signatures": sigs}})
        self.assertEqual(r.status_code, 200, r.data)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNED)
        _mine()
        conf, paid = _paid_to(bond.spend_txid, back)
        self.assertGreaterEqual(conf, 1)
        self.assertGreater(paid, bond.required_sats * 0.8)  # minus the lab's mining fee
        r = self._call("get", order, loser)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["lost_take_done"])

    def _race_in_the_mempool(self, maker_sells):
        from api.escrow import bonds
        from api.models import OnchainBond, TakeOrder

        order, maker, ln_taker, km = self._make(maker_sells=maker_sells)
        loser, keys, bond = self._onchain_take(order)
        self.assertEqual(bond.status, OnchainBond.Status.FUNDING)
        order = self._ln_take(order, ln_taker)
        self.assertEqual(order.status, Order.Status.WF2)
        self.assertEqual(order.taker, ln_taker)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.ABANDONED)  # free, not penalized
        self.assertFalse(bond.penalty_tx)
        self.assertFalse(TakeOrder.objects.filter(order=order, taker=loser, expires_at__gt=timezone.now()).exists())
        self.notify.delay.assert_any_call(order_id=order.id, message=f"take_lost:{loser.id}")
        # the page: only its bond, waiting for the confirmation
        r = self._call("get", order, loser)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["lost_take"])
        self.assertTrue(r.data["lost_take_taken"])
        self.assertFalse(r.data["lost_take_done"])
        self.assertTrue(r.data["bond_mempool_txid"])
        self.assertNotIn("escrow_address", r.data)
        self.assertNotIn("maker_nick", r.data)
        # no new bond for this take
        r = self._call("post", order, loser, {"action": "bond_key", "escrow": {"bond_pubkey": pubkey(keys["bond"]).hex()}})
        self.assertEqual(r.status_code, 403, r.data)
        # one confirmation later it is free to return
        _mine()
        bonds.recover_abandoned()
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)
        r = self._call("get", order, loser)
        self.assertEqual(r.data["bond_status_code"], OnchainBond.Status.RETURNABLE)
        self._get_bond_back(order, loser, keys, bond)
        return order

    def test_case_a_loser_in_the_mempool_gets_its_bond_back(self):
        order = self._race_in_the_mempool(maker_sells=True)
        self.assertEqual(order.escrow_type, Order.EscrowTypes.ONCHAIN)

    def test_case_b_loser_in_the_mempool_gets_its_bond_back(self):
        # the order becomes a Lightning order: before 0.9.8 the loser's bond waited forever
        order = self._race_in_the_mempool(maker_sells=False)
        self.assertEqual(order.escrow_type, Order.EscrowTypes.LIGHTNING)

    def test_loser_already_confirmed_is_free_at_once(self):
        from api.models import OnchainBond

        order, maker, ln_taker, km = self._make(maker_sells=True)
        loser, keys, bond = self._onchain_take(order, confirm=True)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.SIGN)  # its app had not signed yet
        order = self._ln_take(order, ln_taker)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)
        self._get_bond_back(order, loser, keys, bond)

    def test_loser_confirmed_but_unseen_is_free_at_once(self):
        """Confirmed after the last advance: release_take_bond picks the coins up itself."""
        from api.escrow import flow
        from api.logics import Logics
        from api.models import OnchainBond

        order, maker, ln_taker, km = self._make(maker_sells=False)
        loser = _user()
        Logics.take(order, loser)
        keys = self._keys(order, f"loser-{loser.id}")
        flow.action(order, loser, "bond_key", {"bond_pubkey": pubkey(keys["bond"]).hex()})
        d = flow.order_data(order, loser)
        _faucet("sendtoaddress", d["bond_address"], d["bond_required_sats"] / 1e8)
        _mine()
        bond = flow.bond_of(order, "taker", owner=loser)
        self.assertEqual(bond.status, OnchainBond.Status.FUNDING)
        order = self._ln_take(order, ln_taker)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)
        self._get_bond_back(order, loser, keys, bond)

    def test_two_onchain_takers_first_confirmed_wins(self):
        from api.models import OnchainBond

        order, maker, _, km = self._make(maker_sells=True)
        slow, kslow, bslow = self._onchain_take(order)  # stays in the mempool
        fast = _user()
        from api.logics import Logics

        Logics.take(order, fast)
        self._pay_bond(order, fast, self._keys(order, f"fast-{fast.id}"))  # confirms, signs, locks
        order.refresh_from_db()
        self.assertEqual(order.taker, fast)
        self.assertFalse(order.taker_lightning)
        bslow.refresh_from_db()
        # _pay_bond mined a block: the slow bond was picked up before being freed
        self.assertEqual(bslow.status, OnchainBond.Status.RETURNABLE)
        self.notify.delay.assert_any_call(order_id=order.id, message=f"take_lost:{slow.id}")
        self._get_bond_back(order, slow, kslow, bslow)

    def test_reverse_lightning_pretaker_untouched_when_onchain_wins(self):
        """The Lightning pretaker that has not paid yet keeps its take (upstream: its hold
        invoice is cancelled if it ever locks); no take_lost notice for it."""
        from api.logics import Logics
        from api.models import TakeOrder

        order, maker, ln_pre, km = self._make(maker_sells=True)
        Logics.take(order, ln_pre, lightning=True)
        winner = _user()
        Logics.take(order, winner)
        self._pay_bond(order, winner, self._keys(order, f"w-{winner.id}"))
        order.refresh_from_db()
        self.assertEqual(order.taker, winner)
        self.assertTrue(TakeOrder.objects.filter(order=order, taker=ln_pre, expires_at__gt=timezone.now()).exists())
        for c in self.notify.delay.call_args_list:
            self.assertNotEqual(c.kwargs.get("message"), f"take_lost:{ln_pre.id}")
        # its page: the usual 403 of a taken order (it has no onchain bond here)
        self.assertEqual(self._call("get", order, ln_pre).status_code, 403)

    def test_expired_take_in_case_b_is_freed(self):
        """A take that simply expired after the order became a Lightning order (case B):
        clean_orders frees its bond by the take, not by the order's escrow type."""
        from api.escrow import bonds
        from api.logics import Logics
        from api.models import OnchainBond, TakeOrder

        order, maker, ln_taker, km = self._make(maker_sells=False)
        loser, keys, bond = self._onchain_take(order)
        take = TakeOrder.objects.get(order=order, taker=loser)
        order = self._ln_take(order, ln_taker)
        take.refresh_from_db()
        Logics.take_order_expires(take)  # idempotent after lose_race
        take.delete()
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.ABANDONED)
        _mine()
        bonds.recover_abandoned()
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)
        self._get_bond_back(order, loser, keys, bond)

    def test_two_takers_confirmed_in_the_same_block_oldest_wins(self):
        """Kilombino: "what if two takers confirm in the same block?" Both bonds confirm in
        one block and both apps sign before the next pass: the robot that took first wins."""
        from api.escrow import flow
        from api.escrow.bonds import penalty_message
        from api.logics import Logics
        from api.models import OnchainBond

        order, maker, _, km = self._make(maker_sells=True)
        takers = []
        for name in ("first", "second"):
            u = _user()
            Logics.take(order, u)
            k = self._keys(order, f"{name}-{u.id}")
            flow.action(order, u, "bond_key", {"bond_pubkey": pubkey(k["bond"]).hex()})
            d = flow.order_data(order, u)
            _faucet("sendtoaddress", d["bond_address"], d["bond_required_sats"] / 1e8)
            takers.append((u, k))
        _mine()  # one block confirms both
        flow.advance(order)
        bonds_ = [flow.bond_of(order, "taker", owner=u) for u, _ in takers]
        for b in bonds_:
            b.refresh_from_db()
            self.assertEqual(b.status, OnchainBond.Status.SIGN)
        for (u, k), b in zip(reversed(takers), reversed(bonds_)):  # the younger signs first
            _, msgs = penalty_message(b)
            sigs = [with_hashtype(schnorr_sign(k["bond"], m)).hex() for m in msgs]
            flow.action(order, u, "bond_penalty_signature", {"signatures": sigs})
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.WF2)
        self.assertEqual(order.taker, takers[0][0])
        bonds_[1].refresh_from_db()
        self.assertEqual(bonds_[1].status, OnchainBond.Status.RETURNABLE)
        self.notify.delay.assert_any_call(order_id=order.id, message=f"take_lost:{takers[1][0].id}")
        self._get_bond_back(order, takers[1][0], takers[1][1], bonds_[1])


class EscrowLegacyPayoutRegtest(EscrowOrderFlowRegtest):
    """0.9.9: payout/refund addresses of the legacy types (Kilombino: "1…" and "3…" too) and the
    deposit change at the seller's payout address. The buyer gets paid to a P2PKH address, the
    seller's refund (and its deposit change) goes to a P2SH one; every release, the rescue and
    the cancel are mined with the outputs the inherited tests expect."""

    CHANGE_TO_PAYOUT = True
    SELLER_EXTRA = 5_000  # sent on top of the recommended amount: it must come back by itself

    def _payout_address(self, role):
        return _faucet("getnewaddress", "", "legacy" if role == "buyer" else "p2sh-segwit")

    def _funding_amount(self, d, role):
        return d["escrow_funding_recommended"] + (self.SELLER_EXTRA if role == "seller" else 0)

    def test_payout_scripts_are_legacy(self):
        from api.escrow import flow

        order, buyer, seller, keys = self._to_chat()
        escrow = flow.escrow_of(order)
        self.assertTrue(escrow.funding_signatures["buyer_payout_spk"].startswith("76a914"))  # P2PKH
        self.assertTrue(escrow.funding_signatures["seller_payout_spk"].startswith("a914"))  # P2SH

    def test_deposit_change_goes_to_the_seller_payout_address(self):
        from api.escrow import flow

        order, buyer, seller, keys = self._to_chat()
        escrow = flow.escrow_of(order)
        plan = escrow.funding_signatures["plan"]
        self.assertEqual(plan["change_to"].get("seller"), "payout")
        self.assertEqual(plan["extra_in_escrow"], {})
        info = service.rpc("getrawtransaction", [escrow.funding_txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        seller_spk = escrow.funding_signatures["seller_payout_spk"]
        change = [round(o["value"] * 1e8) for o in info["vout"] if o["scriptPubKey"]["hex"] == seller_spk]
        seller_in = sum(int(plan["spent"][i][0]) for i in plan["seller_inputs"])
        self.assertEqual(change, [seller_in - plan["seller_pays"]])
        self.assertGreater(change[0], self.SELLER_EXTRA)  # the extra and the margin, by itself
        # nothing left at the funding address: no "Get my sats back" afterwards
        self.assertEqual(flow.funding_leftover(order, escrow, "seller"), [])
        self.assertTrue(flow.order_data(order, seller)["escrow_change_to_payout"])


class EscrowFairDeadlineRegtest(EscrowOrderFlowRegtest):
    """0.9.9: a trader that signs its part late does not leave its peer the minutes it did not
    use: the peer gets at least an hour (ESCROW_PEER_STEP_SECONDS)."""

    test_happy_path_to_successful = None
    test_collaborative_cancel = None
    test_dispute_for_the_buyer_from_the_admin = None
    test_dispute_return_everything_from_the_admin = None
    test_taker_never_funds_is_penalized = None
    test_fair_rescue_without_the_coordinator = None
    test_rescue_plan_1_kept_for_old_escrows = None

    def _sign_rescue(self, order, users, keys):
        from api.escrow import flow
        from api.escrow.taproot import Transaction, TxOut

        escrow = flow.escrow_of(order)
        order.refresh_from_db()
        self.after_build = order.expires_at
        self.assertGreater(order.expires_at, timezone.now() + timezone.timedelta(minutes=55))
        # the seller signs with ten minutes left: the buyer gets an hour
        order.expires_at = timezone.now() + timezone.timedelta(minutes=10)
        order.save(update_fields=["expires_at"])
        for r in ("seller", "buyer"):
            d = flow.order_data(order, users[r])
            c = service.contract_for(escrow)
            tx = Transaction.parse(bytes.fromhex(d["escrow_rescue_tx"]))
            msg = tx.sighash_unified(0, [TxOut(escrow.escrow_sats, c.script_pubkey)], 0x21, c.leaf_hash(c.RESCUE))
            flow.action(order, users[r], "escrow_rescue_signature",
                        {"signature": with_hashtype(schnorr_sign(keys[r]["escrow"], msg)).hex()})
            order.refresh_from_db()
            if r == "seller":
                self.assertGreater(order.expires_at, timezone.now() + timezone.timedelta(minutes=59))
        escrow.refresh_from_db()
        self.extensions = escrow.funding_signatures.get("deadline_extensions")
        self.rescue_hex = service.signed_rescue(escrow).serialize().hex()
        self.rescue_plan_data = escrow.funding_signatures["rescue"]

    def test_the_late_signer_gives_its_peer_an_hour(self):
        order, buyer, seller, keys = self._to_chat()
        self.assertGreaterEqual(self.extensions, 1)
        self.assertLessEqual(self.extensions, 6)


class EscrowLegacyBondReturnRegtest(EscrowAutoReturnRegtest):
    """0.9.9: a bond return to a legacy address, automatic and by hand."""

    def _return_address(self, user):
        if not hasattr(self, "return_addresses"):
            self.return_addresses = {}
        self.return_addresses.setdefault(user.id, _faucet("getnewaddress", "", "legacy"))
        return self.return_addresses[user.id]

    test_taker_bond_freed_when_the_maker_cancels = None
    test_bond_in_a_deposit_is_never_returned_again = None

    def test_manual_return_to_a_legacy_address(self):
        from api.escrow import bonds, flow
        from api.logics import Logics
        from api.models import OnchainBond

        self.SIGN_AUTO_RETURN = False
        order, buyer, kb, bond = self._make_public()
        Logics.cancel_order(order, buyer)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNABLE)
        back = _faucet("getnewaddress", "", "legacy")
        flow.action(order, buyer, "bond_return", {"address": back})
        bond.refresh_from_db()
        tx = Transaction.parse(bytes.fromhex(bond.return_tx))
        msgs = bonds._coin_messages(bond, tx, bonds.return_coins_of(bond))
        flow.action(order, buyer, "bond_return_signature",
                    {"signatures": [with_hashtype(schnorr_sign(kb["bond"], m)).hex() for m in msgs]})
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNED)
        _mine()
        conf, paid = _paid_to(bond.spend_txid, back)
        self.assertGreaterEqual(conf, 1)
        info = service.rpc("getrawtransaction", [bond.spend_txid, True])
        self.assertEqual(len(info["vout"]), 1)
        self.assertTrue(info["vout"][0]["scriptPubKey"]["hex"].startswith("76a914"))  # P2PKH
        self.assertGreater(paid, bond.value - 3_000)


class EscrowLegacyPayoutSwappedRegtest(EscrowLegacyPayoutRegtest):
    """0.9.9, the other way round: the buyer is paid to a P2SH ("3…") address and the seller's
    refund is P2PKH ("1…"): a dispute won by the buyer releases to the 3… address and "return
    everything" refunds the seller to the 1… one (both mined, outputs exact)."""

    test_payout_scripts_are_legacy = None
    test_deposit_change_goes_to_the_seller_payout_address = None
    test_fair_rescue_without_the_coordinator = None
    test_rescue_plan_1_kept_for_old_escrows = None
    test_collaborative_cancel = None
    test_taker_never_funds_is_penalized = None

    def _payout_address(self, role):
        return _faucet("getnewaddress", "", "p2sh-segwit" if role == "buyer" else "legacy")

    def test_scripts(self):
        from api.escrow import flow

        order, buyer, seller, keys = self._to_chat()
        escrow = flow.escrow_of(order)
        self.assertTrue(escrow.funding_signatures["buyer_payout_spk"].startswith("a914"))
        self.assertTrue(escrow.funding_signatures["seller_payout_spk"].startswith("76a914"))


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowDisputePageRegtest(EscrowOrderFlowRegtest):
    """0.9.9-1: an on-chain dispute resolved from the coordinator's DISPUTE PAGE (not the
    changelist action): until 0.9.9 the page refused every on-chain dispute ("the seller's
    escrow has not been settled by the coordinator"). The page's resolve path runs the action
    under the order lock; the signer's app signs; the release is mined with what the page said."""

    test_happy_path_to_successful = None
    test_collaborative_cancel = None
    test_dispute_for_the_buyer_from_the_admin = None
    test_dispute_return_everything_from_the_admin = None
    test_taker_never_funds_is_penalized = None

    def _page(self, order, decision):
        from unittest.mock import patch

        from django.test import Client
        from django.urls import reverse

        from roblosats_admin import disputes

        admin = User.objects.create_superuser(f"panel-{uuid.uuid4().hex[:6]}", "a@example.org", "pw")
        client = Client()
        client.force_login(admin)
        page = reverse("admin:api_order_dispute", args=[order.pk])
        order.refresh_from_db()
        plan = disputes.plan(order, decision)
        self.assertTrue(plan["possible"], plan["missing"])
        body = client.get(f"{page}?confirm={decision}", HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertNotIn("has not been settled by the coordinator", body)
        with patch("api.admin.send_notification"):
            r = client.post(page, {"action": "resolve", "decision": decision, "confirmed": "yes",
                                   "status": str(order.status)})
        self.assertEqual(r.status_code, 302)
        msgs = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any(m.startswith("Done:") for m in msgs), msgs)
        order.refresh_from_db()
        return plan, msgs, client, page

    def test_return_everything_from_the_page(self):
        from api.escrow import flow
        from api.logics import Logics

        order, buyer, seller, keys = self._to_chat()
        order.expires_at = timezone.now()
        order.save()
        self.assertEqual(Logics.open_dispute(order, buyer), (True, None))
        plan, msgs, client, page = self._page(order, "return")
        self.assertEqual(order.status, Order.Status.CCA)
        self.assertTrue(any("Waiting for the seller's signature" in m for m in msgs), msgs)
        escrow = flow.escrow_of(order)
        self.assertEqual((escrow.status, escrow.release_kind),
                         (OnchainEscrow.Status.RELEASING, OnchainEscrow.ReleaseKind.SELLER_WINS))
        # #26: the buyer waits for the seller, not for "the coordinator"/"together"
        self.assertEqual(flow.order_data(order, buyer)["escrow_waiting_for"], "peer")
        self.assertEqual(flow.order_data(order, seller)["escrow_waiting_for"], "you")
        _, msg = service.release_message(escrow)
        flow.action(order, seller, "escrow_leaf_signature",
                    {"signature": with_hashtype(schnorr_sign(keys["seller"]["escrow"], msg)).hex()})
        outs, mining = self._release_outputs(escrow)
        want_seller = 1_000_000 + 30_000 + self.seller_fee + self.extra["seller"]
        want_buyer = self.buyer_in + self.buyer_fee + self.extra["buyer"]
        self.assertEqual(outs, {"seller": want_seller - mining, "buyer": want_buyer})
        # what the page announced is what was paid (the seller's line is before the mining fee)
        text = " ".join(plan["lines"])
        from roblosats_admin.disputes import fmt_sats

        self.assertIn(fmt_sats(want_seller), text)
        self.assertIn(fmt_sats(want_buyer), text)
        self.assertIn(escrow.funding_signatures["buyer_payout_address"], text)
        # the page now says it was broadcast; the buyer's summary says where its bond went
        self.assertIn("broadcast the release", client.get(page, HTTP_ACCEPT_LANGUAGE="en").content.decode())
        s = __import__("api.order_summary", fromlist=["x"]).final_summary(order, buyer)
        self.assertEqual(s["bond_state"], "returned")
        self.assertEqual(s["bond_returned_to"], escrow.funding_signatures["buyer_payout_address"])

    def test_buyer_wins_from_the_page(self):
        from api.escrow import flow
        from api.logics import Logics

        order, buyer, seller, keys = self._to_chat()
        order.expires_at = timezone.now()
        order.save()
        self.assertEqual(Logics.open_dispute(order, seller), (True, None))
        plan, msgs, client, page = self._page(order, "buyer")
        self.assertEqual(order.status, Order.Status.TLD)  # the taker (seller) lost
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.release_kind, OnchainEscrow.ReleaseKind.BUYER_WINS)
        self.assertEqual(flow.order_data(order, seller)["escrow_waiting_for"], "peer")
        _, msg = service.release_message(escrow)
        flow.action(order, buyer, "escrow_leaf_signature",
                    {"signature": with_hashtype(schnorr_sign(keys["buyer"]["escrow"], msg)).hex()})
        outs, mining = self._release_outputs(escrow)
        outs_plan, info = flow.dispute_split(order, escrow, True)
        want = dict(outs_plan)
        want["buyer"] -= mining
        self.assertEqual(outs, want)
        from roblosats_admin.disputes import fmt_sats

        text = " ".join(plan["lines"])
        self.assertIn(fmt_sats(dict(outs_plan)["buyer"]), text)
        self.assertIn(fmt_sats(dict(outs_plan)["coordinator"]), text)

    # 0.9.10: penalize with no trade and a dispute solved as a completed trade, from the page
    def _opened(self, by="buyer"):
        from api.logics import Logics

        order, buyer, seller, keys = self._to_chat()
        order.expires_at = timezone.now()
        order.save()
        self.assertEqual(Logics.open_dispute(order, buyer if by == "buyer" else seller), (True, None))
        return order, buyer, seller, keys

    def _sign(self, order, user, keys, role):
        from api.escrow import flow

        escrow = flow.escrow_of(order)
        _, msg = service.release_message(escrow)
        flow.action(order, user, "escrow_leaf_signature",
                    {"signature": with_hashtype(schnorr_sign(keys[role]["escrow"], msg)).hex()})
        escrow.refresh_from_db()
        return escrow

    def _check_penalize(self, guilty):
        from api.escrow import flow

        order, buyer, seller, keys = self._opened()
        plan, msgs, client, page = self._page(order, f"penalize_{guilty}")
        # BUY order: the maker buys
        self.assertEqual(order.status, Order.Status.MLD if guilty == "buyer" else Order.Status.TLD)
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.release_kind, OnchainEscrow.ReleaseKind.SELLER_WINS)
        self.assertEqual(flow.order_data(order, seller)["escrow_waiting_for"], "you")
        self.assertEqual(flow.order_data(order, buyer)["escrow_waiting_for"], "peer")
        split, info = flow.penalize_split(order, escrow, guilty)
        escrow = self._sign(order, seller, keys, "seller")
        outs, mining = self._release_outputs(escrow)
        want = dict(split)
        payer = escrow.funding_signatures["release_fee_payer"]
        want[payer] -= mining
        self.assertEqual(outs, {k: v for k, v in want.items() if v >= 330})
        from roblosats_admin.disputes import fmt_sats

        text = " ".join(plan["lines"])
        for who, sats in split:
            self.assertIn(fmt_sats(sats), text, who)
        self.assertEqual(flow.proceeds(order), 0)  # no trade: no fee
        return order, buyer, seller, info

    def test_penalize_seller_from_the_page(self):
        order, buyer, seller, info = self._check_penalize("seller")
        s = __import__("api.order_summary", fromlist=["x"]).final_summary(order, buyer)
        self.assertEqual((s["bond_state"], s["rewards_sats"]), ("returned", info["reward"]))
        s = __import__("api.order_summary", fromlist=["x"]).final_summary(order, seller)
        self.assertEqual(s["bond_state"], "lost")

    def test_penalize_buyer_from_the_page(self):
        order, buyer, seller, info = self._check_penalize("buyer")
        s = __import__("api.order_summary", fromlist=["x"]).final_summary(order, buyer)
        self.assertEqual(s["bond_state"], "lost")

    def test_completed_from_the_page(self):
        from api.escrow import flow

        order, buyer, seller, keys = self._opened(by="seller")
        plan, msgs, client, page = self._page(order, "completed")
        self.assertEqual(order.status, Order.Status.PAY)
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.release_kind, OnchainEscrow.ReleaseKind.BUYER_WINS)
        split, info = flow.completed_split(order, escrow)
        escrow = self._sign(order, buyer, keys, "buyer")
        outs, mining = self._release_outputs(escrow)
        want = dict(split)
        want["buyer"] -= mining
        self.assertEqual(outs, want)
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)
        self.assertEqual(flow.proceeds(order), info["coordinator"])
        s = __import__("api.order_summary", fromlist=["x"]).final_summary(order, seller)
        self.assertEqual(s["bond_state"], "returned")


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowAutoReturnLadderRegtest(EscrowAutoReturnRegtest):
    """0.9.11 (auto-return plan 2): the app pre-signs the return at several rates; the
    coordinator sends the cheapest one the mempool needs and, if it gets stuck, replaces it with
    the next (RBF). Plan 1 paid the penalty's rate (up to 10 sat/vB) whatever the mempool."""

    test_cancelled_public_order_returns_the_bond_by_itself = None
    test_taker_bond_freed_when_the_maker_cancels = None
    test_bond_in_a_deposit_is_never_returned_again = None

    def _sign_auto_return(self, order, user, keys):
        from api.escrow import bonds, flow

        d = flow.order_data(order, user)
        if not d.get("bond_auto_return_tx"):
            return
        self.assertGreaterEqual(len(d["bond_auto_return_ladder"]), 2)
        self.assertEqual(d["bond_auto_return_ladder"][-1], d["bond_auto_return_tx"])
        bond = flow.bond_of(order, d["bond_role"], owner=user)

        def sign(tx_hex):
            _, msgs = bonds._rung_message(bond, tx_hex)
            return [with_hashtype(schnorr_sign(keys["bond"], m)).hex() for m in msgs]

        payload = {"signatures": sign(d["bond_auto_return_tx"])}
        if not getattr(self, "_top_only", False):
            payload["ladder"] = [sign(t) for t in d["bond_auto_return_ladder"]]
        flow.action(order, user, "bond_auto_return_signature", payload)
        self.assertTrue(flow.order_data(order, user)["bond_auto_return_signed"])

    def _fee_of(self, txid):
        info = service.rpc("getmempoolentry", [txid])
        return info["fees"]["base"] * 1e8 / info["vsize"]

    def test_cheap_rung_at_low_fees_then_bumped(self):
        from unittest.mock import patch

        from api.escrow import bonds
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, kb, bond = self._make_public()
        bond.refresh_from_db()
        rates = [r["rate"] for r in bond.auto_return["ladder"]]
        self.assertEqual(rates[:2], [1.0, 3.0])
        with patch("api.escrow.bonds.target_rate", return_value=1.0):
            Logics.cancel_order(order, buyer)
        bond.refresh_from_db()
        self.assertEqual(bond.status, OnchainBond.Status.RETURNED)
        self.assertEqual(bond.auto_return["sent"]["rate"], 1.0)
        first = bond.spend_txid
        self.assertLess(self._fee_of(first), 1.5)
        # still unconfirmed long after: the next rung replaces it (same coins, RBF)
        with patch("api.escrow.bonds.target_rate", return_value=1.0):
            self.assertFalse(bonds.bump_auto_return(bond))  # not stale yet
            self.assertTrue(bonds.bump_auto_return(bond, now=bond.auto_return["sent"]["at"] + 7300))
        bond.refresh_from_db()
        self.assertNotEqual(bond.spend_txid, first)
        self.assertEqual(bond.auto_return["sent"]["rate"], 3.0)
        self.assertEqual(bond.auto_return["sent"]["replaced"], first)
        mempool = service.rpc("getrawmempool", [])
        self.assertIn(bond.spend_txid, mempool)
        self.assertNotIn(first, mempool)
        self.assertGreater(self._fee_of(bond.spend_txid), 2.5)
        _mine()
        conf, paid = _paid_to(bond.spend_txid, self.return_addresses[buyer.id])
        self.assertGreaterEqual(conf, 1)
        self.assertFalse(bonds.bump_auto_return(bond))  # confirmed: nothing more
        bond.refresh_from_db()
        self.assertTrue(bond.auto_return.get("confirmed"))
        s = __import__("api.order_summary", fromlist=["x"]).final_summary(order, buyer)
        self.assertEqual(s["bond_returned_sats"], paid)
        self.assertEqual(s["mining_fee_sats"], bond.value - paid)

    def test_high_fees_send_the_top_rung(self):
        from unittest.mock import patch

        from api.logics import Logics

        order, buyer, kb, bond = self._make_public()
        bond.refresh_from_db()
        top = bond.auto_return["ladder"][-1]["rate"]
        with patch("api.escrow.bonds.target_rate", return_value=top + 20):
            Logics.cancel_order(order, buyer)
        bond.refresh_from_db()
        self.assertEqual(bond.auto_return["sent"]["rate"], top)
        self.assertEqual(bond.spend_txid, bond.auto_return["sent"]["txid"])
        _mine()
        conf, _paid = _paid_to(bond.spend_txid, self.return_addresses[buyer.id])
        self.assertGreaterEqual(conf, 1)

    def test_top_only_signature_keeps_plan_1_behaviour(self):
        """An app that signs only the top rung (0.9.10 and older) gets plan 1's single return."""
        from unittest.mock import patch

        from api.escrow import bonds
        from api.logics import Logics

        self._top_only = True
        order, buyer, kb, bond = self._make_public()
        bond.refresh_from_db()
        self.assertEqual(len(bonds.signed_rungs(bond)), 1)
        with patch("api.escrow.bonds.target_rate", return_value=1.0):
            Logics.cancel_order(order, buyer)
        bond.refresh_from_db()
        self.assertEqual(bond.auto_return["sent"]["rate"], bond.auto_return["rate"])


def _sign_penalty_ladder(test, order, user, keys, bond):
    """0.9.12: the app signs the penalty AND its fee ladder (one list of signatures per rung)."""
    from api.escrow import bonds, flow
    from api.escrow.bonds import penalty_message

    d = flow.order_data(order, user)
    test.assertEqual(d["bond_penalty_ladder"][-1], d["bond_penalty_tx"])
    test.assertEqual(d["bond_penalty_rates"][:2], [1.0, 3.0])
    _, msgs = penalty_message(bond)
    sigs = [with_hashtype(schnorr_sign(keys["bond"], m)).hex() for m in msgs]
    ladder = []
    for tx_hex in d["bond_penalty_ladder"]:
        tx = Transaction.parse(bytes.fromhex(tx_hex))
        ladder.append([with_hashtype(schnorr_sign(keys["bond"], m)).hex()
                       for m in bonds._coin_messages(bond, tx, bonds.coins_of(bond))])
    flow.action(order, user, "bond_penalty_signature", {"signature": sigs[0], "signatures": sigs, "ladder": ladder})
    flow.advance(order)
    order.refresh_from_db()


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowDisputeBond0912Regtest(EscrowHybridRegtest):
    """0.9.12 (Kilombino, TG 14873): an onchain bond is not penalized when a dispute opens.
    Hybrid B: the maker's onchain bond stays locked until the decision, goes back on chain to a
    winning maker (its signed return, cheapest rung) and is penalized (cheapest penalty rung)
    when the maker loses; the timelock safety guard penalizes it before its owner could take it
    back alone; a decision after the owner took it back penalizes nothing. Pure onchain: the
    bonds sit in the escrow, nothing is broadcast at the opening (the decision's release splits
    them, see EscrowDisputePageRegtest)."""

    AUTO_RETURN = True
    SIGN_AUTO_RETURN = True
    test_case_a_lightning_buyer_to_successful = None
    test_case_a_small_order_one_minimum_in_total = None
    test_case_b_proxy_return_and_penalty_onchain = None

    def _sign_penalty(self, order, user, keys, bond):
        _sign_penalty_ladder(self, order, user, keys, bond)

    def _sign_auto_return(self, order, user, keys):
        from api.escrow import bonds, flow

        d = flow.order_data(order, user)
        if not d.get("bond_auto_return_tx"):
            return
        bond = flow.bond_of(order, d["bond_role"], owner=user)

        def sign(tx_hex):
            _, msgs = bonds._rung_message(bond, tx_hex)
            return [with_hashtype(schnorr_sign(keys["bond"], m)).hex() for m in msgs]

        flow.action(order, user, "bond_auto_return_signature",
                    {"signatures": sign(d["bond_auto_return_tx"]), "ladder": [sign(t) for t in d["bond_auto_return_ladder"]]})

    def _disputed_b(self, csv=None):
        """A hybrid B order (the maker buys onchain, the seller took with Lightning) in the chat,
        then the chat expires: the dispute opens by itself (#29)."""
        import os
        from unittest.mock import patch

        from api.escrow import flow
        from api.logics import Logics

        env = {"ESCROW_LAB_BOND_CSV": str(csv)} if csv else {}
        with patch.dict(os.environ, env):
            order, buyer, seller, kb = self._make(maker_sells=False)
        order = self._ln_take(order, seller)
        self.assertTrue(flow.has_proxy(order))
        now = timezone.now()
        order.trade_escrow = LNPayment.objects.create(
            concept=LNPayment.Concepts.TRESCROW, type=LNPayment.Types.HOLD, sender=seller,
            status=LNPayment.Status.LOCKED, num_satoshis=1_039_106, payment_hash=uuid.uuid4().hex,
            preimage=uuid.uuid4().hex, invoice=f"lnbcrt{uuid.uuid4().hex}", created_at=now,
            expires_at=now + timezone.timedelta(hours=3),
        )
        from api.models import OnchainPayment

        order.is_swap = True  # the buyer gave an onchain payout address (#29)
        order.payout_tx = OnchainPayment.objects.create(
            concept=OnchainPayment.Concepts.PAYBUYER, status=OnchainPayment.Status.VALID, balance=None,
            address=_faucet("getnewaddress", "", "bech32"), num_satoshis=999_800, sent_satoshis=999_800,
            receiver=buyer,
        )
        order.status = Order.Status.CHA
        order.expires_at = now - timezone.timedelta(minutes=1)
        order.save()
        bond = flow.bond_of(order, "maker")
        self.assertEqual(len(bond.penalty_ladder.get("sigs") or []), len(bond.penalty_ladder["rungs"]))
        mempool = set(service.rpc("getrawmempool", []))
        with patch("api.logics.LNNode") as ln:
            ln.settle_hold_invoice.return_value = True
            Logics.order_expires(order)  # CHA timeout -> dispute
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DIS)
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.LOCKED)  # NOT penalized at the opening
        self.assertEqual(set(service.rpc("getrawmempool", [])) - mempool, set())  # nothing broadcast
        order.maker_bond.refresh_from_db()
        self.assertEqual(order.maker_bond.status, LNPayment.Status.LOCKED)
        return order, buyer, seller, kb, bond

    def _decide(self, order, decision):
        from unittest.mock import patch

        from django.contrib import admin as dj_admin

        from roblosats_admin import disputes

        staff = User.objects.create_user(username=f"staff_{uuid.uuid4().hex[:6]}", password="x", is_staff=True)
        with patch("api.admin.send_notification"), patch("api.logics.Logics.pay_buyer", return_value=True), \
                patch("api.escrow.bonds.target_rate", return_value=1.0), \
                patch("api.escrow.bonds.penalty_target_rate", return_value=1.0):
            ok, msgs = disputes.resolve(dj_admin.site._registry[Order], Order.objects.get(pk=order.pk), decision, staff)
        self.assertTrue(ok, msgs)
        order.refresh_from_db()

    def _fee_rate(self, txid):
        info = service.rpc("getrawtransaction", [txid, True])
        spent = 0
        for i in info["vin"]:
            prev = service.rpc("getrawtransaction", [i["txid"], True])
            spent += round(prev["vout"][i["vout"]]["value"] * 1e8)
        return (spent - sum(round(o["value"] * 1e8) for o in info["vout"])) / info["vsize"]

    def test_maker_wins_gets_its_bond_back_on_chain_cheaply(self):
        order, buyer, seller, kb, bond = self._disputed_b()
        self._decide(order, "buyer")  # the maker is the buyer
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.RETURNED)
        self.assertEqual(bond.auto_return["sent"]["rate"], 1.0)
        _mine()
        conf, paid = _paid_to(bond.spend_txid, self.return_addresses[buyer.id])
        self.assertGreaterEqual(conf, 1)
        self.assertLess(self._fee_rate(bond.spend_txid), 1.5)
        self.assertGreater(paid, bond.value - 200)  # ~110 sats of mining, not 1,500
        from api.logics import ensure_ledger
        from django.db import connection

        ensure_ledger()
        with connection.cursor() as c:
            c.execute("SELECT SUM(amount) FROM coord_reward_ledger WHERE order_id = %s AND robot_id = %s",
                      [order.id, buyer.robot.id])
            credited = int(c.fetchone()[0] or 0)
        self.assertEqual(credited, 999_800)  # the trade amount only: the bond went back on chain

    def test_maker_loses_bond_penalized_at_the_decision_cheaply(self):
        order, buyer, seller, kb, bond = self._disputed_b()
        self._decide(order, "seller")
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.PENALIZED)
        self.assertEqual(bond.penalty_ladder["sent"]["rate"], 1.0)
        order.maker_bond.refresh_from_db()
        self.assertEqual(order.maker_bond.status, LNPayment.Status.SETLED)
        _mine()
        info = service.rpc("getrawtransaction", [bond.spend_txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        from api.escrow import flow

        self.assertEqual(info["vout"][0]["scriptPubKey"]["hex"], flow.coordinator_fee_spk().hex())
        self.assertLess(self._fee_rate(bond.spend_txid), 1.5)
        from api.escrow import bonds

        self.assertGreater(bonds.penalty_sats(bond), bonds.penalty_floor_sats(bond))
        self.assertEqual(order.status, Order.Status.MLD)

    def test_safety_guard_penalizes_before_the_timelock(self):
        from unittest.mock import patch

        from api.escrow import bonds

        order, buyer, seller, kb, bond = self._disputed_b(csv=30)
        self.assertEqual(bond.refund_csv, 30)
        self.assertEqual(bonds.safety_blocks(bond), 7)
        with patch("api.escrow.bonds.penalty_target_rate", return_value=1.0):
            self.assertEqual(bonds.guard_disputed_bonds(force=True), 0)
            left = bonds.blocks_to_refund(bond)
            _mine(left - 7)  # 7 blocks left: not yet
            self.assertEqual(bonds.guard_disputed_bonds(force=True), 0)
            _mine(1)  # 6 left < 7
            self.assertEqual(bonds.guard_disputed_bonds(force=True), 1)
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.PENALIZED)
        _mine()
        self.assertGreaterEqual(service.rpc("getrawtransaction", [bond.spend_txid, True])["confirmations"], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DIS)
        self.assertIn("safety", order.logs)
        # the coordinator then finds for the maker: its bond's value comes back as rewards
        from api.escrow import flow

        self.assertEqual(flow.dispute_bond_sats(order, "maker", wins=True), bond.value)

    def test_decided_after_the_owner_took_it_back_alone(self):
        from unittest.mock import patch

        from api.escrow import bonds
        from api.escrow.taproot import SIGHASH_ALL_UNIFIED, TxOut, leaf_witness
        from api.escrow.transactions import coins_spend_tx

        order, buyer, seller, kb, bond = self._disputed_b(csv=12)
        _mine(12)  # the guard never ran (the coordinator was down): the timelock is over
        c = bonds.contract_of(bond)
        back = _faucet("getnewaddress", "", "bech32m")
        tx = coins_spend_tx(bonds.coins_of(bond), _spk(back), 2.0, refund=True, csv=c.refund_csv)
        spent = [TxOut(bond.value, c.script_pubkey)]
        msg = tx.sighash_unified(0, spent, SIGHASH_ALL_UNIFIED, c.leaf_hash(c.REFUND))
        tx.inputs[0].witness = leaf_witness(c, c.REFUND, [schnorr_sign(kb["bond"], msg)])
        service.rpc("sendrawtransaction", [tx.serialize().hex()])
        _mine()
        with patch("api.escrow.bonds.penalty_target_rate", return_value=1.0):
            self._decide(order, "seller")  # the maker loses, but its bond is gone
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.RETURNED)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.MLD)
        self.assertIn("no longer at its address", order.logs)

    def test_decided_after_the_timelock_bond_still_there(self):
        """The timelock passed but the owner did not move it: the decision still penalizes."""
        order, buyer, seller, kb, bond = self._disputed_b(csv=12)
        _mine(15)
        self._decide(order, "seller")
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.PENALIZED)
        _mine()
        self.assertGreaterEqual(service.rpc("getrawtransaction", [bond.spend_txid, True])["confirmations"], 1)


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowDisputeOnchain0912Regtest(EscrowDisputePageRegtest):
    """0.9.12, pure onchain: both bonds are inside the escrow when a dispute opens; nothing is
    broadcast then, and the decision's release returns the winner's bond (and its reward share
    of the loser's) and pays the rest of the loser's bond to the coordinator. The penalty
    ladder signed by a new app is kept for the bonds before the deposit."""

    test_return_everything_from_the_page = None

    def _sign_penalty(self, order, user, keys, bond):
        _sign_penalty_ladder(self, order, user, keys, bond)

    def _open(self):
        from api.escrow import flow
        from api.logics import Logics
        from api.models import OnchainBond

        order, buyer, seller, keys = self._to_chat()
        order.expires_at = timezone.now()
        order.save()
        mempool = set(service.rpc("getrawmempool", []))
        self.assertEqual(Logics.open_dispute(order, seller), (True, None))
        self.assertEqual(set(service.rpc("getrawmempool", [])) - mempool, set())
        for role in ("maker", "taker"):
            self.assertEqual(flow.bond_of(order, role).status, OnchainBond.Status.IN_DEPOSIT)
        order.refresh_from_db()
        self.assertIn("inside the onchain escrow", order.logs)
        return order, buyer, seller, keys

    def _release(self, order, signer, key):
        from api.escrow import flow

        escrow = flow.escrow_of(order)
        _, msg = service.release_message(escrow)
        flow.action(order, signer, "escrow_leaf_signature", {"signature": with_hashtype(schnorr_sign(key, msg)).hex()})
        return escrow

    def test_maker_wins_bond_back_in_the_release(self):
        from api.escrow import flow

        order, buyer, seller, keys = self._open()  # the maker is the buyer
        self._page(order, "buyer")
        escrow = self._release(order, buyer, keys["buyer"]["escrow"])
        outs, mining = self._release_outputs(escrow)
        plan, info = flow.dispute_split(order, escrow, True)
        self.assertEqual(outs["buyer"], dict(plan)["buyer"] - mining)
        self.assertEqual(info["bonds"]["buyer"], self.buyer_in)  # its whole bond is in its output
        self.assertLessEqual(escrow.release_fee_rate, 25)

    def test_maker_loses_bond_to_the_coordinator_in_the_release(self):
        from api.escrow import flow

        order, buyer, seller, keys = self._open()
        self._page(order, "seller")
        escrow = self._release(order, seller, keys["seller"]["escrow"])
        outs, mining = self._release_outputs(escrow)
        plan, info = flow.dispute_split(order, escrow, False)
        self.assertEqual(outs.get("coordinator"), dict(plan)["coordinator"])
        self.assertNotIn("buyer", outs)  # the losing maker gets nothing back
