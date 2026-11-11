"""
Onchain escrow service against a regtest node with the BLAKE2b fork (opt-in).

Runs only with ESCROW_REGTEST=1 and BITCOIND_RPCURL pointing at a regtest Knots node whose
fork is active and that has a funded wallet called "faucet" (the lab in
/mnt/datos2/roblosats/lab-onchain). The traders' clients are played in Python with the same
keys a real client derives from its token.
"""

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
    """The order state machine with an onchain escrow: WF2 → WFE → CHA → FSE → PAY → SUC,
    and a dispute solved from the admin for the buyer."""

    def _bond(self, user, concept):
        now = timezone.now()
        return LNPayment.objects.create(
            type=LNPayment.Types.HOLD, concept=concept, status=LNPayment.Status.LOCKED,
            payment_hash=uuid.uuid4().hex, preimage=uuid.uuid4().hex, num_satoshis=30_000,
            created_at=now, expires_at=now + timezone.timedelta(hours=1), sender=user,
        )

    def _to_chat(self):
        from api.escrow import flow

        buyer, seller = _user(), _user()
        order = _order(buyer, seller)
        order.maker_bond = self._bond(buyer, LNPayment.Concepts.MAKEBOND)
        order.taker_bond = self._bond(seller, LNPayment.Concepts.TAKEBOND)
        order.save()
        flow.on_contract(order)
        alias = service.coordinator_alias()
        keys = {r: (derive_secret(f"f-{r}-{order.id}".encode(), alias, order.id, r),
                    derive_secret(f"f-{r}-{order.id}".encode(), alias, order.id, "funding"))
                for r in ("seller", "buyer")}
        for r, u in (("seller", seller), ("buyer", buyer)):
            flow.action(order, u, "escrow_keys", {"escrow_pubkey": pubkey(keys[r][0]).hex(), "funding_pubkey": pubkey(keys[r][1]).hex()})
        flow.action(order, buyer, "escrow_address", {"address": _faucet("getnewaddress", "", "bech32m")})
        flow.action(order, seller, "escrow_address", {"address": _faucet("getnewaddress", "", "bech32m")})
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.WFE)
        for r, u in (("seller", seller), ("buyer", buyer)):
            d = flow.order_data(order, u)
            _faucet("sendtoaddress", d["escrow_funding_address"], d["escrow_funding_recommended"] / 1e8)
        _mine()
        flow.advance(order)
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.status, OnchainEscrow.Status.FUNDING)
        for r, u in (("seller", seller), ("buyer", buyer)):
            d = flow.order_data(order, u)
            from api.escrow.taproot import Transaction, TxOut
            tx = Transaction.parse(bytes.fromhex(d["escrow_deposit_tx"]))
            spent = [TxOut(v, bytes.fromhex(s)) for v, s in d["escrow_deposit_spent"]]
            sigs = [with_hashtype(schnorr_sign(keys[r][1], tx.sighash_unified(i, spent))).hex() for i in d["escrow_deposit_my_inputs"]]
            flow.action(order, u, "escrow_funding_signatures", {"signatures": sigs})
        _mine()
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CHA)
        return order, buyer, seller, keys

    def test_happy_path_to_successful(self):
        from unittest.mock import patch
        from api.escrow import flow
        from api.logics import Logics

        order, buyer, seller, keys = self._to_chat()
        with patch("api.logics.LNNode") as node, patch("api.logics.send_devfund_donation"), patch("api.logics.send_notification"):
            node.cancel_return_hold_invoice.return_value = True
            self.assertEqual(Logics.confirm_fiat(order, buyer), (True, None))
            order.refresh_from_db()
            self.assertEqual(Logics.confirm_fiat(order, seller), (True, None))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAY)
        c = service.contract_for(flow.escrow_of(order))
        _, msg = service.release_message(flow.escrow_of(order))
        pks, tw, isx = c.musig_pubkeys(), [c.tweak], [True]
        secs = {}
        for i, (r, u) in enumerate((("seller", seller), ("buyer", buyer))):
            sec, pub = M.nonce_gen(keys[r][0], pks[i], c.output_key, msg, None)
            secs[r] = sec
            flow.action(order, u, "escrow_nonce", {"nonce": pub.hex()})
        agg = bytes.fromhex(flow.order_data(order, buyer)["escrow_release_aggnonce"])
        ctx = M.SessionContext(agg, pks, tw, isx, msg)
        with patch("api.logics.send_devfund_donation"):
            for r, u in (("seller", seller), ("buyer", buyer)):
                flow.action(order, u, "escrow_partial", {"partial": M.sign(secs[r], keys[r][0], ctx).hex()})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)
        self.assertEqual(order.proceeds, flow.escrow_of(order).coordinator_fee_sats)

    def test_dispute_for_the_buyer_from_the_admin(self):
        from unittest.mock import MagicMock, patch
        from api.admin import OrderAdmin
        from api.escrow import flow
        from api.logics import Logics
        from django.contrib.admin.sites import AdminSite

        order, buyer, seller, keys = self._to_chat()
        order.expires_at = timezone.now()
        order.save()
        with patch("api.logics.LNNode") as node, patch("api.logics.send_notification"):
            node.settle_hold_invoice.return_value = True
            node.double_check_htlc_is_settled.return_value = True
            self.assertEqual(Logics.open_dispute(order, buyer), (True, None))
        admin = OrderAdmin(Order, AdminSite())
        admin.message_user = MagicMock()
        with patch("api.admin.send_notification"):
            admin.taker_wins(MagicMock(), Order.objects.filter(pk=order.pk)) if order.taker == buyer else admin.maker_wins(MagicMock(), Order.objects.filter(pk=order.pk))
        escrow = flow.escrow_of(order)
        self.assertEqual(escrow.status, OnchainEscrow.Status.RELEASING)
        _, msg = service.release_message(escrow)
        flow.action(order, buyer, "escrow_leaf_signature", {"signature": with_hashtype(schnorr_sign(keys["buyer"][0], msg)).hex()})
        _mine()
        escrow.refresh_from_db()
        self.assertEqual(escrow.status, OnchainEscrow.Status.RELEASED)
        self.assertGreaterEqual(service.rpc("getrawtransaction", [escrow.release_txid, True])["confirmations"], 1)
