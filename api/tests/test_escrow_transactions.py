"""
Deposit and release transactions of the onchain escrow (api/escrow/transactions.py).

The same code ran end to end on regtest with the BLAKE2b fork (lab-onchain/py/lab_flow.py,
16/16): joint deposit with the fees inside, cooperative key-path release and dispute release,
with estimated sizes equal to the real ones and the requested fee rate reached.
"""

from django.test import SimpleTestCase

from api.escrow.taproot import EscrowContract, TxOut, derive_secret, pubkey
from api.escrow.transactions import (
    DUST_P2TR,
    VB_KEYPATH,
    DepositError,
    Utxo,
    check_deposit,
    funding_descriptor,
    funding_script_pubkey,
    plan_deposit,
    release_tx,
)

SPK = lambda n: b"\x51\x20" + bytes([n]) * 32


def _setup():
    keys = [derive_secret(b"t", "kilobot", 1, r) for r in ("seller", "buyer", "coordinator", "funding")]
    c = EscrowContract(pubkey(keys[0]), pubkey(keys[1]), pubkey(keys[2]), 4480)
    return c, funding_script_pubkey(pubkey(keys[3]))


def _plan(seller_value=1_020_000, buyer_value=3_000, dev=400, rate=3.0):
    c, fs = _setup()
    us = [Utxo("aa" * 32, 0, seller_value, fs)]
    ub = [Utxo("bb" * 32, 1, buyer_value, SPK(9))] if buyer_value else []
    return c, fs, plan_deposit(us, ub, c, 1_000_000, 1_700, 200, SPK(1), dev, SPK(2), fs, SPK(9), rate)


class DepositPlanTest(SimpleTestCase):
    def test_outputs_and_fee_split(self):
        c, _fs, p = _plan()
        outs = {o.script_pubkey: o.value for o in p.tx.outputs}
        self.assertEqual(p.tx.outputs[0].script_pubkey, c.script_pubkey)
        self.assertEqual(p.tx.outputs[0].value, 1_000_000)
        self.assertEqual(outs[SPK(1)], 1_700 + 200 - 400)
        self.assertEqual(outs[SPK(2)], 400)
        self.assertEqual(p.seller_pays + p.buyer_pays, 1_000_000 + 1_900 + p.mining_fee)
        self.assertGreaterEqual(p.mining_fee, 3.0 * p.tx.vsize() - 1)

    def test_dev_below_dust_goes_to_coordinator(self):
        _, _, p = _plan(dev=DUST_P2TR - 1)
        values = [o.value for o in p.tx.outputs]
        self.assertIn(1_900, values)
        self.assertNotIn(SPK(2), [o.script_pubkey for o in p.tx.outputs])

    def test_short_funding_is_refused_with_the_missing_amount(self):
        with self.assertRaisesRegex(DepositError, "seller's funding is short by"):
            _plan(seller_value=1_001_000)
        with self.assertRaisesRegex(DepositError, "buyer's funding is short by"):
            _plan(buyer_value=250)

    def test_dust_change_is_left_as_fee(self):
        _c, fs, p = _plan(seller_value=1_001_700 + 700)
        self.assertNotIn(fs, [o.script_pubkey for o in p.tx.outputs])

    def test_descriptor(self):
        _, fs = _setup()
        self.assertEqual(funding_descriptor(b"\x02" + fs[2:]), f"rawtr({fs[2:].hex()})")


class CheckDepositTest(SimpleTestCase):
    def test_accepts_the_plan(self):
        c, fs, p = _plan()
        check_deposit(p.tx, p.spent, c, 1_000_000, p.seller_inputs, 1_000_000 + 1_700 + 2_000, fs, 10)

    def test_refuses_wrong_escrow_or_overspend_or_fee(self):
        c, fs, p = _plan()
        with self.assertRaises(DepositError):
            check_deposit(p.tx, p.spent, c, 999_999, p.seller_inputs, 10**7, fs, 10)
        with self.assertRaises(DepositError):
            check_deposit(p.tx, p.spent, c, 1_000_000, p.seller_inputs, 1_000_000, fs, 10)
        with self.assertRaises(DepositError):
            check_deposit(p.tx, p.spent, c, 1_000_000, p.seller_inputs, 10**7, fs, 1)
        p.tx.outputs[0] = TxOut(1_000_000, SPK(7))
        with self.assertRaises(DepositError):
            check_deposit(p.tx, p.spent, c, 1_000_000, p.seller_inputs, 10**7, fs, 10)


class ReleaseTest(SimpleTestCase):
    def test_buyer_pays_the_fee(self):
        c, _ = _setup()
        tx = release_tx(c, "cc" * 32, 0, 1_000_000, [TxOut(1_000_000, SPK(5))], VB_KEYPATH, 2.0)
        self.assertEqual(tx.outputs[0].value, 1_000_000 - 224)

    def test_payouts_must_match_and_dust_is_dropped(self):
        c, _ = _setup()
        with self.assertRaises(ValueError):
            release_tx(c, "cc" * 32, 0, 1_000_000, [TxOut(999_999, SPK(5))], VB_KEYPATH, 2.0)
        tx = release_tx(c, "cc" * 32, 0, 1_000_000, [TxOut(999_800, SPK(5)), TxOut(200, SPK(6))], VB_KEYPATH, 2.0)
        self.assertEqual(len(tx.outputs), 1)
