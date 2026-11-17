"""Roblosats rl31-beta16: fair rescue (rescue plan 2).

Up to rl31-beta15 leaf 2 of the escrow was <seller> CSV: once the timelock passed, the seller
alone took the whole escrow, the buyer's bond (and held fee) included. Rescue plan 2 changes
leaf 2 to <seller> CHECKSIGVERIFY <buyer> CHECKSIGVERIFY <csv> CSV and both traders pre-sign,
before the deposit, a rescue transaction with fixed outputs: the seller gets the trade amount,
its bond and its held fee; the buyer its bond and its held fee. Without the coordinator either
side can broadcast it once the CSV passed; neither can take the other's part. Escrows created
before keep plan 1 (no "rescue_plan" in funding_signatures).
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from api.escrow import service
from api.escrow.taproot import (
    SIGHASH_ALL_UNIFIED,
    EscrowContract,
    Transaction,
    TxOut,
    derive_secret,
    leaf_witness,
    pubkey,
    schnorr_sign,
    schnorr_verify,
    xonly,
)
from api.escrow.transactions import DUST_P2TR, rescue_tx

SELLER_SPK = bytes.fromhex("5120" + "a1" * 32)
BUYER_SPK = bytes.fromhex("0014" + "b2" * 20)



def _keys():
    return (
        derive_secret(b"token-del-vendedor", "kilobot", 4242, "seller"),
        derive_secret(b"token-del-comprador", "kilobot", 4242, "buyer"),
        derive_secret(b"secreto-del-coordinador", "kilobot", 4242, "coordinator"),
    )


def _contract(plan=2, csv=2016):
    s, b, c = _keys()
    return EscrowContract(pubkey(s), pubkey(b), pubkey(c), csv, rescue_plan=plan)


class RescuePlanContract(SimpleTestCase):
    def test_plan_1_unchanged(self):
        self.assertEqual(
            _contract(1, 4480).address,
            "bc1p9jhzqnjypwjcwvleg862n460lgdmqrvkh98rzefgedum6yrk7z8qp8xsxd",
        )

    def test_plan_2_leaf_needs_both_and_the_timelock(self):
        c = _contract()
        s, b, _ = _keys()
        leaf = c.leaves[c.RESCUE]
        self.assertEqual(
            leaf.hex(),
            "20" + xonly(pubkey(s)).hex() + "ad" + "20" + xonly(pubkey(b)).hex() + "ad" + "02e007" + "b2",
        )
        self.assertNotEqual(c.address, _contract(1, 2016).address)
        # the dispute leaves do not change
        self.assertEqual(c.leaves[:2], _contract(1, 2016).leaves[:2])

    def test_pinned_address(self):  # same value in frontend escrow.test.ts
        self.assertEqual(
            _contract().address,
            "bc1pu9kcewkmrtdex8uhx7f9d665wcv8xr8yefl36ae3zr8z8afavqpsvghja6",
        )


class RescueTransaction(SimpleTestCase):
    def test_each_side_gets_its_own_minus_half_the_fee(self):
        c = _contract()
        tx = rescue_tx(c, "11" * 32, 0, 125_000, 110_000, SELLER_SPK, 15_000, BUYER_SPK, 2.0)
        self.assertEqual(tx.inputs[0].sequence, 2016)
        self.assertEqual(tx.version, 2)
        fee = 125_000 - sum(o.value for o in tx.outputs)
        self.assertEqual([o.script_pubkey for o in tx.outputs], [SELLER_SPK, BUYER_SPK])
        self.assertEqual(110_000 - tx.outputs[0].value, fee - fee // 2)
        self.assertEqual(15_000 - tx.outputs[1].value, fee // 2)
        self.assertLess(fee, 2.0 * 200)

    def test_buyer_with_nothing_inside(self):  # hybrid A: Lightning buyer bond
        tx = rescue_tx(_contract(), "11" * 32, 0, 50_000, 50_000, SELLER_SPK, 0, BUYER_SPK, 1.0)
        self.assertEqual(len(tx.outputs), 1)
        self.assertEqual(tx.outputs[0].script_pubkey, SELLER_SPK)

    def test_dust_buyer_goes_to_the_miners_not_the_seller(self):
        tx = rescue_tx(_contract(), "11" * 32, 0, 50_300, 50_000, SELLER_SPK, 300, BUYER_SPK, 1.0)
        self.assertEqual(len(tx.outputs), 1)
        self.assertLess(tx.outputs[0].value, 50_000)

    def test_must_add_up(self):
        with self.assertRaises(ValueError):
            rescue_tx(_contract(), "11" * 32, 0, 50_000, 40_000, SELLER_SPK, 9_000, BUYER_SPK, 1.0)

    def test_signed_by_both_verifies_with_leaf_2(self):
        c = _contract()
        s, b, _ = _keys()
        tx = rescue_tx(c, "11" * 32, 0, 125_000, 110_000, SELLER_SPK, 15_000, BUYER_SPK, 2.0)
        msg = tx.sighash_unified(0, [TxOut(125_000, c.script_pubkey)], SIGHASH_ALL_UNIFIED,
                                 c.leaf_hash(c.RESCUE))
        ss, sb = schnorr_sign(s, msg), schnorr_sign(b, msg)
        self.assertTrue(schnorr_verify(msg, xonly(pubkey(s)), ss))
        self.assertTrue(schnorr_verify(msg, xonly(pubkey(b)), sb))
        w = leaf_witness(c, c.RESCUE, [sb, ss])
        self.assertEqual(w[2], c.leaves[2])
        self.assertEqual(len(w[3]), 65)  # one step: leaf 2 is next to branch (0, 1)
        # a different output set is a different message: the pre-signed outputs are fixed
        tx2 = rescue_tx(c, "11" * 32, 0, 125_000, 125_000, SELLER_SPK, 0, BUYER_SPK, 2.0)
        msg2 = tx2.sighash_unified(0, [TxOut(125_000, c.script_pubkey)], SIGHASH_ALL_UNIFIED,
                                   c.leaf_hash(c.RESCUE))
        self.assertFalse(schnorr_verify(msg2, xonly(pubkey(b)), sb))


class _Escrow:
    """Enough of an OnchainEscrow for the service helpers."""

    def __init__(self, data, csv=2016):
        s, b, c = _keys()
        self.seller_pubkey, self.buyer_pubkey, self.coordinator_pubkey = (
            pubkey(s).hex(), pubkey(b).hex(), pubkey(c).hex())
        self.rescue_csv = csv
        self.funding_signatures = data
        self.escrow_sats = 125_000
        self.funding_tx = Transaction.parse(bytes.fromhex(_deposit_hex())).serialize().hex()

    def save(self, **kw):
        pass


def _deposit_hex():
    from api.escrow.taproot import TxIn

    return Transaction([TxIn("33" * 32, 1)], [TxOut(125_000, _contract().script_pubkey)]).serialize().hex()


class RescueService(SimpleTestCase):
    def test_versions(self):
        self.assertEqual(service.rescue_plan(_Escrow({})), 1)
        self.assertEqual(service.rescue_plan(_Escrow({"rescue_plan": 2})), 2)
        with patch("api.escrow.service.network_hrp", return_value="bc"):
            self.assertEqual(service.contract_for(_Escrow({})).leaves[2], _contract(1).leaves[2])
            self.assertEqual(service.contract_for(_Escrow({"rescue_plan": 2})).leaves[2], _contract(2).leaves[2])

    def test_build_and_complete(self):
        e = _Escrow({"rescue_plan": 2, "seller_payout_spk": SELLER_SPK.hex(), "buyer_payout_spk": BUYER_SPK.hex()})
        with patch("api.escrow.service.network_hrp", return_value="bc"), \
             patch("api.escrow.service.fee_rate", return_value=1.0):
            rtx = service.build_rescue(e, 110_000, 15_000)
            self.assertEqual(rtx.inputs[0].txid, Transaction.parse(bytes.fromhex(e.funding_tx)).txid())
            self.assertIsNone(service.signed_rescue(e))
            c, tx, msg = service.rescue_message(e)
            s, b, _ = _keys()
            e.funding_signatures["seller_rescue_sig"] = (schnorr_sign(s, msg) + bytes([SIGHASH_ALL_UNIFIED])).hex()
            e.funding_signatures["buyer_rescue_sig"] = (schnorr_sign(b, msg) + bytes([SIGHASH_ALL_UNIFIED])).hex()
            full = service.signed_rescue(e)
            self.assertEqual(len(full.inputs[0].witness), 4)
            self.assertGreaterEqual(min(o.value for o in full.outputs), DUST_P2TR)
