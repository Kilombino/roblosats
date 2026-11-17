"""Roblosats rl31-beta18: onchain bonds paid in more than one payment, and coins at a bond
address that are not the bond.

Kilobot order #22 (17 nov 2026): the bond was 3,330 sats, the maker sent 3,300 by mistake and
the coordinator only took a single coin of at least the bond, so the 3,300 stayed at the bond
address with no way back but the owner's refund leaf (and the app did not offer even that).
Now:
- a bond locks with the smallest single confirmed coin that reaches it, else with the fewest
  largest coins that together reach it (top-up, at most MAX_BOND_COINS); the penalty, the
  deposit and the return spend all its coins, one owner signature per coin;
- any other coin at the bond address (a short payment that was not needed, an extra or late
  payment) is the owner's: the coordinator co-signs its return to an address of the owner's at
  any time (bond_leftover_return / bond_leftover_signatures), and the owner takes it alone
  through the refund leaf after the bond's CSV.

Unit tests run anywhere; the regtest ones need the lab (ESCROW_REGTEST=1, see
test_escrow_service_regtest.py).
"""

import math
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from api.escrow import bonds
from api.escrow.taproot import (
    BondContract,
    EscrowContract,
    Transaction,
    TxOut,
    derive_secret,
    leaf_witness,
    pubkey,
    schnorr_sign,
    with_hashtype,
)
from api.escrow.transactions import (
    DUST_P2TR,
    VB_BOND_COOP,
    VB_BOND_REFUND,
    VB_OVERHEAD,
    BondInput,
    Utxo,
    coins_spend_tx,
    output_vbytes,
    plan_deposit,
)
from api.tests import test_escrow_service_regtest as base
from api.tests.test_escrow_service_regtest import REGTEST, _faucet, _mine, _spk

T = ["aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32]


class ChooseCoins(SimpleTestCase):
    def test_short_then_full_locks_with_the_full_one(self):
        # order #22: 3,300 first, then 3,330: the bond is the 3,330 coin, the 3,300 is left over
        coins = [(T[0], 0, 3300), (T[1], 0, 3330)]
        self.assertEqual(bonds.choose_coins(coins, 3330), [(T[1], 0, 3330)])

    def test_smallest_single_coin_that_reaches(self):
        coins = [(T[0], 0, 10_000), (T[1], 1, 3_400), (T[2], 0, 3_330)]
        self.assertEqual(bonds.choose_coins(coins, 3330), [(T[2], 0, 3_330)])

    def test_top_up_adds_up(self):
        coins = [(T[0], 0, 3300), (T[1], 0, 330)]
        self.assertEqual(bonds.choose_coins(coins, 3330), [(T[0], 0, 3300), (T[1], 0, 330)])

    def test_not_enough_yet(self):
        self.assertIsNone(bonds.choose_coins([(T[0], 0, 3300)], 3330))
        self.assertIsNone(bonds.choose_coins([], 3330))

    def test_at_most_max_coins(self):
        coins = [(T[i], 0, 1000) for i in range(4)]
        self.assertEqual(bonds.MAX_BOND_COINS, 3)
        self.assertIsNone(bonds.choose_coins(coins, 3500))
        self.assertEqual(len(bonds.choose_coins(coins, 3000)), 3)


class _FakeBond:
    """Just what bonds.leftovers / coins_of read."""

    class Status:
        FUNDING, SIGN, LOCKED, IN_DEPOSIT, PENALIZED, RETURNABLE, RETURNED, ABANDONED = range(8)

    def __init__(self, status, coins, **kw):
        self.status = status
        self.coins = [list(c) for c in coins]
        self.txid = coins[0][0] if coins else ""
        self.vout = coins[0][1] if coins else None
        self.value = sum(c[2] for c in coins)
        self.return_coins = kw.get("return_coins", [])
        self.leftover_txids = kw.get("leftover_txids", [])
        self.address = "bcrt1p..."


class Leftovers(SimpleTestCase):
    def setUp(self):
        p = patch("api.models.OnchainBond.Status", _FakeBond.Status)
        p.start()
        self.addCleanup(p.stop)

    def test_coins_that_are_not_the_bond(self):
        S = _FakeBond.Status
        here = [(T[0], 0, 3300), (T[1], 0, 3330), (T[2], 5, 1000)]
        b = _FakeBond(S.LOCKED, [(T[1], 0, 3330)])
        self.assertEqual(bonds.leftovers(b, here), [(T[0], 0, 3300), (T[2], 5, 1000)])

    def test_none_while_waiting_or_when_the_return_takes_them(self):
        S = _FakeBond.Status
        here = [(T[0], 0, 3300)]
        for st in (S.FUNDING, S.ABANDONED, S.RETURNABLE):
            self.assertEqual(bonds.leftovers(_FakeBond(st, []), here), [])

    def test_after_the_bond_went_away_and_not_twice(self):
        S = _FakeBond.Status
        b = _FakeBond(S.IN_DEPOSIT, [(T[1], 0, 3330)],
                      leftover_txids=[{"txid": T[3], "coins": [[T[0], 0, 3300]], "sats": 3000}])
        # the bond coin (spent by the deposit, maybe still in the mempool) and a coin already
        # on its way back never show again; a new late payment does
        here = [(T[0], 0, 3300), (T[1], 0, 3330), (T[2], 1, 5000)]
        self.assertEqual(bonds.leftovers(b, here), [(T[2], 1, 5000)])


def _bond_contract(n=1):
    owner = derive_secret(b"tok", "kilobot", 22, f"bond{n}")
    coord = derive_secret(b"coord", "kilobot", 22, f"bond{n}")
    return BondContract(pubkey(owner), pubkey(coord), 540), owner, coord


class SpendCoins(SimpleTestCase):
    def test_return_of_several_coins(self):
        spk = bytes.fromhex("5120" + "11" * 32)
        coins = [(T[0], 0, 3300), (T[1], 2, 3330)]
        tx = coins_spend_tx(coins, spk, 2.0)
        vb = math.ceil(VB_OVERHEAD + 2 * VB_BOND_COOP + output_vbytes(spk))
        self.assertEqual([(i.txid, i.vout) for i in tx.inputs], [(T[0], 0), (T[1], 2)])
        self.assertEqual(tx.outputs[0].value, 6630 - math.ceil(vb * 2.0))

    def test_refund_leaf_sequence(self):
        spk = bytes.fromhex("5120" + "11" * 32)
        tx = coins_spend_tx([(T[0], 0, 3300)], spk, 1.0, refund=True, csv=540)
        self.assertEqual(tx.inputs[0].sequence, 540)
        vb = math.ceil(VB_OVERHEAD + VB_BOND_REFUND + output_vbytes(spk))
        self.assertEqual(tx.outputs[0].value, 3300 - vb)

    def test_too_small(self):
        with self.assertRaises(ValueError):
            coins_spend_tx([(T[0], 0, 400)], bytes.fromhex("5120" + "11" * 32), 5.0)


class DepositWithToppedUpBond(SimpleTestCase):
    def test_every_bond_coin_is_an_input_and_counts_in_the_escrow(self):
        keys = [derive_secret(b"k", "kilobot", 1, r) for r in ("s", "b", "c")]
        contract = EscrowContract(*(pubkey(k) for k in keys), 20)
        bc, _, _ = _bond_contract()
        seller_spk = bytes.fromhex("5120" + "22" * 32)
        buyer_spk = bytes.fromhex("5120" + "33" * 32)
        seller = [Utxo(T[3], 0, 120_000, seller_spk)]
        sbond = BondInput(Utxo(T[0], 0, 3000, bc.script_pubkey), bc)
        bbond = BondInput(Utxo(T[1], 0, 3300, bc.script_pubkey), bc, [Utxo(T[2], 1, 330, bc.script_pubkey)])
        self.assertEqual(bbond.value, 3630)
        plan = plan_deposit(seller, [], contract, 100_000, 1000, 1000, b"", 0, None, seller_spk, buyer_spk, 2.0,
                            sbond, bbond, buyer_fee_from_bond=True, fees_in_escrow=True)
        self.assertEqual(plan.bond_inputs, {"seller": 1, "buyer": 2})
        self.assertEqual(plan.bond_input_lists, {"seller": [1], "buyer": [2, 3]})
        self.assertEqual([(i.txid, i.vout) for i in plan.tx.inputs], [(T[3], 0), (T[0], 0), (T[1], 0), (T[2], 1)])
        # the buyer's fee and mining share (two bond inputs) come out of its two coins
        buyer_mining = math.ceil((2 * VB_BOND_COOP + (VB_OVERHEAD + output_vbytes(contract.script_pubkey)) / 2) * 2.0)
        self.assertEqual(plan.bond_in_escrow["buyer"], 3630 - 1000 - buyer_mining)
        self.assertEqual(plan.tx.outputs[0].value, 100_000 + 2000 + 3000 + plan.bond_in_escrow["buyer"])


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class BondTopUpRegtest(base.EscrowOrderFlowRegtest):
    """Against the lab node: the real penalty, deposit, returns and refund leaf."""

    def _new_order(self):
        from api.models import Currency, Order
        from api.tests.test_escrow_service_regtest import _user

        buyer, seller = _user(), _user()
        now = timezone.now()
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        order = Order.objects.create(
            currency=currency, type=Order.Types.BUY, status=Order.Status.WFB, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100,
            satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=buyer, bond_size=3, is_explicit=True,
        )
        return order, buyer, seller

    def _open(self, order, user, keys):
        from api.escrow import flow

        flow.action(order, user, "bond_key", {"bond_pubkey": pubkey(keys["bond"]).hex()})
        return flow.order_data(order, user)

    def _send(self, address, sats):
        txid = _faucet("sendtoaddress", address, sats / 1e8)
        _mine()
        return txid

    def _mined_out(self, txid, spk_hex):
        info = service_rpc("getrawtransaction", [txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        return info, sum(round(o["value"] * 1e8) for o in info["vout"] if o["scriptPubKey"]["hex"] == spk_hex)

    def _return_leftover(self, order, user, keys):
        """The cooperative return of what is not the bond, as the client does it."""
        from api.escrow import flow

        from api.escrow import service

        addr = _faucet("getnewaddress", "", "bech32m")
        with patch.object(service, "fee_rate", return_value=2.0):
            flow.action(order, user, "bond_leftover_return", {"address": addr})
        d = flow.order_data(order, user)
        bond = flow.bond_of(order, d["bond_role"], owner=user)
        tx = Transaction.parse(bytes.fromhex(d["bond_leftover_tx"]))
        coins = [(c["txid"], c["vout"], c["value"]) for c in d["bond_leftover_coins"]]
        c = bonds.contract_of(bond)
        spent = [TxOut(v, c.script_pubkey) for _, _, v in coins]
        sigs = [with_hashtype(schnorr_sign(keys["bond"], tx.sighash_unified(i, spent, 0x21, c.leaf_hash(c.COOPERATIVE)))).hex()
                for i in range(len(tx.inputs))]
        flow.action(order, user, "bond_leftover_signatures", {"signatures": sigs})
        _mine()
        bond.refresh_from_db()
        txid = bond.leftover_txids[-1]["txid"]
        info, got = self._mined_out(txid, _spk(addr).hex())
        vb = math.ceil(VB_OVERHEAD + len(coins) * VB_BOND_COOP + output_vbytes(_spk(addr)))
        self.assertEqual(got, sum(v for _, _, v in coins) - math.ceil(vb * 2.0))  # exact, at 2 sat/vB
        self.assertLessEqual(math.ceil(info["vsize"]), vb)
        return coins, got, info

    def test_underpay_then_top_up_and_trade(self):
        """3 % bond of 1,000,000 = 30,000: 29,000 first (stays waiting), then 1,000 more: the bond
        locks with both coins, the penalty needs two signatures, the deposit spends both and the
        trade completes with exact outputs."""
        from api.escrow import flow
        from api.logics import Logics
        from api.models import Order

        order, buyer, seller = self._new_order()
        kb, ks = self._keys(order, "buyer"), self._keys(order, "seller")
        d = self._open(order, buyer, kb)
        self.assertEqual(d["bond_required_sats"], 30_000)
        self._send(d["bond_address"], 29_000)
        flow.advance(order)
        d = flow.order_data(order, buyer)
        self.assertEqual(d["bond_status_code"], 0)
        self.assertEqual(d["bond_confirmed_sats"], 29_000)
        self.assertEqual(d["bond_max_coins"], 3)
        # the top-up seen in the mempool: together with the rest it reaches the bond
        _faucet("sendtoaddress", d["bond_address"], 1_000 / 1e8)
        seen = flow.mempool_seen(order)
        self.assertEqual([(s["sats"], s["short"]) for s in seen if s["what"] == "bond"], [(1000, 0)])
        _mine()
        flow.advance(order)
        bond = flow.bond_of(order, "maker")
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.SIGN)
        self.assertEqual(sorted(c[2] for c in bond.coins), [1000, 29_000])
        self.assertEqual(bond.value, 30_000)
        pen = Transaction.parse(bytes.fromhex(bond.penalty_tx))
        self.assertEqual(len(pen.inputs), 2)
        # one signature is not enough
        from api.escrow.bonds import penalty_message

        _, msgs = penalty_message(bond)
        with self.assertRaises(flow.EscrowError):
            flow.action(order, buyer, "bond_penalty_signature",
                        {"signature": with_hashtype(schnorr_sign(kb["bond"], msgs[0])).hex()})
        self._sign_penalty(order, buyer, kb, bond)
        self.assertEqual(order.status, Order.Status.PUB)
        Logics.take(order, seller)
        self._pay_bond(order, seller, ks)
        self.assertEqual(order.status, Order.Status.WF2)

        # the rest of the trade with the usual helpers (they sign every bond coin)
        with patch.object(self, "_make_and_take", lambda: (order, buyer, seller, {"buyer": kb, "seller": ks})):
            order, buyer, seller, keys = self._to_chat()
        escrow = flow.escrow_of(order)
        dep = service_rpc("getrawtransaction", [escrow.funding_txid, True])
        self.assertTrue({(c[0], c[1]) for c in bond.coins} <= {(v["txid"], v["vout"]) for v in dep["vin"]})
        plan = escrow.funding_signatures["plan"]
        self.assertEqual(len(plan["bond_input_lists"]["buyer"]), 2)
        Logics.confirm_fiat(order, buyer)
        Logics.confirm_fiat(order, seller)
        flow.advance(order)
        self._musig(order, {"buyer": buyer, "seller": seller}, keys)
        escrow = flow.escrow_of(order)
        outs, mining = self._release_outputs(escrow)
        # the buyer (maker) gets the trade amount and what its 2-coin bond left in the escrow
        self.assertEqual(outs["buyer"] + mining, 1_000_000 + self.buyer_in)
        self.assertEqual(outs["seller"], 30_000)
        flow.advance(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SUC)

    def test_order_22_short_coin_left_over_and_late_payment(self):
        """#22: 29,700 then 30,000: locks with the 30,000 alone; the 29,700 comes back right away
        with the coordinator's signature while the order is public; a payment sent after the
        bond went into the deposit comes back the same way."""
        from api.escrow import flow
        from api.logics import Logics
        from api.models import Order

        order, buyer, seller = self._new_order()
        kb, ks = self._keys(order, "buyer"), self._keys(order, "seller")
        d = self._open(order, buyer, kb)
        self._send(d["bond_address"], 29_700)
        flow.advance(order)
        self.assertEqual(flow.bond_of(order, "maker").status, 0)
        full = self._send(d["bond_address"], 30_000)
        flow.advance(order)
        bond = flow.bond_of(order, "maker")
        self.assertEqual(bond.status, bond.Status.SIGN)
        self.assertEqual([c[0] for c in bond.coins], [full])
        self.assertEqual(bond.value, 30_000)
        self._sign_penalty(order, buyer, kb, bond)
        self.assertEqual(order.status, Order.Status.PUB)
        d = flow.order_data(order, buyer)
        self.assertEqual(d["bond_leftover_sats"], 29_700)
        coins, got, _ = self._return_leftover(order, buyer, kb)
        self.assertEqual([c[2] for c in coins], [29_700])
        first_back = got
        d = flow.order_data(order, buyer)
        self.assertNotIn("bond_leftovers", d)
        self.assertEqual(d["bond_leftover_returned_sats"], got)
        # the bond itself was never touched
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.LOCKED)
        self.assertTrue(service_rpc("gettxout", [full, bond.vout]))

        # the trade goes on; a payment after the bond went into the deposit is left over too
        Logics.take(order, seller)
        self._pay_bond(order, seller, ks)
        with patch.object(self, "_make_and_take", lambda: (order, buyer, seller, {"buyer": kb, "seller": ks})):
            order, buyer, seller, keys = self._to_chat()
        self._send(bond.address, 5_000)
        d = flow.order_data(order, buyer)
        self.assertEqual(d["bond_leftover_sats"], 5_000)
        coins, got, _ = self._return_leftover(order, buyer, kb)
        self.assertEqual([c[2] for c in coins], [5_000])
        # and the final summary counts both returns
        from api import order_summary

        s = order_summary.final_summary(order, buyer)
        self.assertEqual(s["bond_extra_returned_sats"], first_back + got)
        self.assertEqual(s["bond_extra_sats"], 0)
        self.assertEqual(len([t for t in s["txs"] if t["label"] == "bond_leftover_return"]), 2)

    def test_overpay_single_coin(self):
        from api.escrow import flow

        order, buyer, _ = self._new_order()
        kb = self._keys(order, "buyer")
        d = self._open(order, buyer, kb)
        self._send(d["bond_address"], 40_000)
        flow.advance(order)
        bond = flow.bond_of(order, "maker")
        self.assertEqual(bond.status, bond.Status.SIGN)
        self.assertEqual(bond.value, 40_000)  # a single coin is never split: the extra rides in the bond
        self.assertEqual(len(bond.coins), 1)

    def test_payment_after_expiry_returns_every_coin(self):
        """The order expires while the bond waits; two payments arrive later (short + top-up):
        both are free and go back in one return."""
        from api.escrow import flow
        from api.logics import Logics

        order, buyer, _ = self._new_order()
        kb = self._keys(order, "buyer")
        d = self._open(order, buyer, kb)
        order.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        order.save()
        Logics.order_expires(order)
        bond = flow.bond_of(order, "maker")
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.ABANDONED)
        self._send(d["bond_address"], 20_000)
        self._send(d["bond_address"], 7_000)
        self.assertEqual(bonds.recover_abandoned(), 1)
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.RETURNABLE)
        self.assertEqual(bond.value, 27_000)
        from api.escrow import service

        addr = _faucet("getnewaddress", "", "bech32m")
        with patch.object(service, "fee_rate", return_value=2.0):
            flow.action(order, buyer, "bond_return", {"address": addr})
        d = flow.order_data(order, buyer)
        tx = Transaction.parse(bytes.fromhex(d["bond_return_tx"]))
        self.assertEqual(len(tx.inputs), 2)
        c = bonds.contract_of(bond)
        coins = [(x["txid"], x["vout"], x["value"]) for x in d["bond_return_coins"]]
        spent = [TxOut(v, c.script_pubkey) for _, _, v in coins]
        sigs = [with_hashtype(schnorr_sign(kb["bond"], tx.sighash_unified(i, spent, 0x21, c.leaf_hash(c.COOPERATIVE)))).hex()
                for i in range(2)]
        flow.action(order, buyer, "bond_return_signature", {"signatures": sigs})
        _mine()
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.RETURNED)
        info, got = self._mined_out(bond.spend_txid, _spk(addr).hex())
        vb = math.ceil(VB_OVERHEAD + 2 * VB_BOND_COOP + output_vbytes(_spk(addr)))
        self.assertEqual(got, 27_000 - math.ceil(vb * 2.0))

    def test_recover_without_the_coordinator_after_the_csv(self):
        """A coin at the bond address that is not the bond: the owner alone, through the refund
        leaf, once the bond's CSV passed (rejected one block before), with the exact output."""
        from api.escrow import flow

        order, buyer, _ = self._new_order()
        kb = self._keys(order, "buyer")
        d = self._open(order, buyer, kb)
        short = self._send(d["bond_address"], 3_300)
        self._send(d["bond_address"], 30_000)
        flow.advance(order)
        bond = flow.bond_of(order, "maker")
        self._sign_penalty(order, buyer, kb, bond)
        bond.refresh_from_db()
        self.assertEqual(bond.status, bond.Status.LOCKED)
        c = bonds.contract_of(bond)
        vout = next(o["n"] for o in service_rpc("getrawtransaction", [short, True])["vout"]
                    if o["scriptPubKey"]["hex"] == bond.script_pubkey)
        dest = _spk(_faucet("getnewaddress", "", "bech32m"))
        tx = coins_spend_tx([(short, vout, 3_300)], dest, 1.0, refund=True, csv=c.refund_csv)
        spent = [TxOut(3_300, c.script_pubkey)]
        sig = schnorr_sign(kb["bond"], tx.sighash_unified(0, spent, 0x21, c.leaf_hash(c.REFUND)))
        tx.inputs[0].witness = leaf_witness(c, c.REFUND, [sig])
        confs = service_rpc("getrawtransaction", [short, True])["confirmations"]
        _mine(c.refund_csv - confs - 1)  # one block short
        with self.assertRaises(Exception) as e:
            service_rpc("sendrawtransaction", [tx.serialize().hex()])
        self.assertIn("non-BIP68-final", str(e.exception))
        _mine(1)
        txid = service_rpc("sendrawtransaction", [tx.serialize().hex()])
        _mine()
        info, got = self._mined_out(txid, dest.hex())
        vb = math.ceil(VB_OVERHEAD + VB_BOND_REFUND + output_vbytes(dest))
        self.assertEqual(got, 3_300 - vb)
        self.assertLessEqual(math.ceil(info["vsize"]), vb)
        # the bond is still there, untouched
        self.assertTrue(service_rpc("gettxout", [bond.txid, bond.vout]))
        self.assertGreaterEqual(DUST_P2TR, 330)


# only our own tests here: the inherited ones run in their own module
for _name in dir(base.EscrowOrderFlowRegtest):
    if _name.startswith("test_") and _name not in BondTopUpRegtest.__dict__:
        setattr(BondTopUpRegtest, _name, None)


def service_rpc(method, params):
    from api.escrow import service

    return service.rpc(method, params)
