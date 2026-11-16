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
    service.rpc("generatetoaddress", [n, _faucet("getnewaddress")])


def _spk(addr):
    return bytes.fromhex(_faucet("getaddressinfo", addr)["scriptPubKey"])


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class EscrowServiceRegtest(TransactionTestCase):
    def _trade(self, dispute):
        buyer, seller = _user(), _user()
        order = _order(buyer, seller)
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
        _, msg = penalty_message(bond)
        flow.action(order, user, "bond_penalty_signature", {"signature": with_hashtype(schnorr_sign(keys["bond"], msg)).hex()})
        flow.advance(order)
        order.refresh_from_db()
        return bond

    def _make_and_take(self):
        from api.escrow import flow
        from api.logics import Logics

        from api.models import Currency

        buyer, seller = _user(), _user()
        now = timezone.now()
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        order = Order.objects.create(
            currency=currency, type=Order.Types.BUY, status=Order.Status.WFB, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100,
            satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=buyer, bond_size=3, is_explicit=True,
        )
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
            _faucet("sendtoaddress", d["escrow_funding_address"], d["escrow_funding_recommended"] / 1e8)
        _mine()
        flow.advance(order)
        self.assertEqual(flow.escrow_of(order).status, OnchainEscrow.Status.FUNDING)
        for r, u in (("seller", seller), ("buyer", buyer)):
            d = flow.order_data(order, u)
            tx = Transaction.parse(bytes.fromhex(d["escrow_deposit_tx"]))
            spent = [TxOut(v, bytes.fromhex(sp)) for v, sp in d["escrow_deposit_spent"]]
            sigs = [with_hashtype(schnorr_sign(keys[r]["funding"], tx.sighash_unified(i, spent))).hex() for i in d["escrow_deposit_my_inputs"]]
            bi = d["escrow_deposit_my_bond_input"]
            from api.escrow.bonds import contract_of
            bc = contract_of(flow.bond_of(order, d["bond_role"], owner=u))
            bsig = with_hashtype(schnorr_sign(keys[r]["bond"], tx.sighash_unified(bi, spent, 0x21, bc.leaf_hash(bc.COOPERATIVE)))).hex()
            flow.action(order, u, "escrow_funding_signatures", {"signatures": sigs, "bond_signature": bsig})
        _mine()
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CHA)
        escrow = flow.escrow_of(order)
        # beta9: the buyer's fee and mining share may have come out of its bond
        self.buyer_in = escrow.funding_signatures["plan"].get("bond_in_escrow", {}).get("buyer", 30_000)
        self.assertEqual(escrow.escrow_sats, 1_000_000 + 30_000 + self.buyer_in)
        return order, buyer, seller, keys

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
        _mine()
        outs = sorted(round(o["value"] * 1e8) for o in service.rpc("getrawtransaction", [escrow.release_txid, True])["vout"])
        info = service.rpc("getrawtransaction", [escrow.release_txid, True])
        self.assertEqual(outs[0], 30_000)
        self.assertEqual(outs[1], 1_000_000 + self.buyer_in - math.ceil(math.ceil(info["vsize"]) * escrow.release_fee_rate))

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
        self.assertEqual(escrow.status, OnchainEscrow.Status.RELEASED)

    def test_dispute_for_the_buyer_from_the_admin(self):
        from unittest.mock import MagicMock, patch
        from api.admin import OrderAdmin
        from api.escrow import flow
        from api.logics import Logics
        from django.contrib.admin.sites import AdminSite

        order, buyer, seller, keys = self._to_chat()
        order.expires_at = timezone.now()
        order.save()
        self.assertEqual(Logics.open_dispute(order, buyer), (True, None))
        admin = OrderAdmin(Order, AdminSite())
        admin.message_user = MagicMock()
        with patch("api.admin.send_notification"):
            admin.maker_wins(MagicMock(), Order.objects.filter(pk=order.pk))
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.status, OnchainEscrow.Status.RELEASING)
        _, msg = service.release_message(escrow)
        flow.action(order, buyer, "escrow_leaf_signature", {"signature": with_hashtype(schnorr_sign(keys["buyer"]["escrow"], msg)).hex()})
        _mine()
        escrow.refresh_from_db()
        outs = sorted(round(o["value"] * 1e8) for o in service.rpc("getrawtransaction", [escrow.release_txid, True])["vout"])
        info = service.rpc("getrawtransaction", [escrow.release_txid, True])
        self.assertEqual(outs[0], 15_000)
        self.assertEqual(outs[1], 1_000_000 + self.buyer_in + 15_000 - math.ceil(math.ceil(info["vsize"]) * escrow.release_fee_rate))

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

    def _make(self, maker_sells):
        from api.models import Currency

        maker, taker = _user(), _user()
        User.objects.get_or_create(username="admin")
        now = timezone.now()
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        order = Order.objects.create(
            currency=currency, type=Order.Types.SELL if maker_sells else Order.Types.BUY, status=Order.Status.WFB,
            escrow_type=Order.EscrowTypes.ONCHAIN, created_at=now, expires_at=now + timezone.timedelta(hours=24),
            has_range=False, amount=100, satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000,
            maker=maker, bond_size=3, is_explicit=True,
        )
        km = self._keys(order, "seller" if maker_sells else "buyer")
        self._pay_bond(order, maker, km)
        self.assertEqual(order.status, Order.Status.PUB)
        return order, maker, taker, km

    def test_case_a_lightning_buyer_to_successful(self):
        from unittest.mock import patch

        from api.escrow import flow
        from api.escrow.bonds import contract_of
        from api.escrow.taproot import Transaction, TxOut
        from api.logics import Logics

        order, seller, buyer, ks = self._make(maker_sells=True)
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
        flow.advance(order)
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.status, OnchainEscrow.Status.FUNDING)
        plan = escrow.funding_signatures["plan"]
        self.assertEqual(list(plan["bond_ids"]), ["seller"])
        self.assertEqual(plan["buyer_inputs"], [])
        seller_fee, buyer_fee = flow.fees(order)
        self.assertEqual(plan["buyer_fee_at_release"], buyer_fee)
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
        self.assertEqual(flow.escrow_of(order).escrow_sats, 1_000_000 + 30_000)
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
        self.assertEqual(values, sorted([buyer_fee, 30_000, 1_000_000 - buyer_fee - mining]))
        self.assertEqual(order.proceeds, seller_fee + buyer_fee)
        summary = flow.trade_summary(order, buyer)
        self.assertTrue(summary["lightning_bond"])
        self.assertTrue(summary["fee_at_release"])
        self.assertEqual(summary["received_sats"], 1_000_000 - buyer_fee - mining)

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
