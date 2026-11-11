"""
Onchain escrow primitives (api/escrow/taproot.py), phase 2 of the onchain deposit.

The vectors were produced by this code and the same code was checked end to end against a
regtest Knots 29.5 node with the BLAKE2b fork (lab in /mnt/datos2/roblosats/lab-onchain,
script py/lab_escrow.py): key path MuSig2(seller, buyer) and every leaf, with 0x21, accepted
by the fork and refused by a node without it. These tests pin them so the client code (JS)
and later changes can be checked against fixed values.
"""

from django.test import SimpleTestCase

from api.escrow import bip327 as M
from api.escrow.taproot import (
    SIGHASH_ALL_UNIFIED,
    EscrowContract,
    Transaction,
    TxIn,
    TxOut,
    derive_secret,
    keypath_witness,
    leaf_witness,
    p2tr_address,
    pubkey,
    schnorr_sign,
    schnorr_verify,
    xonly,
)

S = "03c94775090f921d1865978e21ccf6d0d9d9cd877d71fbb7f93479807adbdf3982"
B = "02f83c9a54f0e918d68b4b9f6a3b3fe4f573007effd865892807916103e37e326e"
C = "020764e0f847d501bfa14e542f49d919f7ad308751f45fb45f5d9b094cc763f032"


def _keys():
    return (
        derive_secret(b"token-del-vendedor", "kilobot", 4242, "seller"),
        derive_secret(b"token-del-comprador", "kilobot", 4242, "buyer"),
        derive_secret(b"secreto-del-coordinador", "kilobot", 4242, "coordinator"),
    )


def _contract(csv=4480):
    sk_s, sk_b, sk_c = _keys()
    return EscrowContract(pubkey(sk_s), pubkey(sk_b), pubkey(sk_c), csv)


def _tx():
    return Transaction(
        [TxIn("11" * 32, 0)], [TxOut(99_000, bytes.fromhex("5120" + "22" * 32))]
    )


class EscrowKeysTest(SimpleTestCase):
    def test_derivation_is_deterministic(self):
        sk_s, sk_b, sk_c = _keys()
        self.assertEqual(pubkey(sk_s).hex(), S)
        self.assertEqual(pubkey(sk_b).hex(), B)
        self.assertEqual(pubkey(sk_c).hex(), C)

    def test_derivation_depends_on_every_field(self):
        base = derive_secret(b"t", "kilobot", 1, "seller")
        self.assertNotEqual(base, derive_secret(b"u", "kilobot", 1, "seller"))
        self.assertNotEqual(base, derive_secret(b"t", "rosegarden", 1, "seller"))
        self.assertNotEqual(base, derive_secret(b"t", "kilobot", 2, "seller"))
        self.assertNotEqual(base, derive_secret(b"t", "kilobot", 1, "buyer"))


class EscrowContractTest(SimpleTestCase):
    def test_address_and_tree(self):
        c = _contract()
        self.assertEqual(
            c.address, "bc1p9jhzqnjypwjcwvleg862n460lgdmqrvkh98rzefgedum6yrk7z8qp8xsxd"
        )
        self.assertEqual(
            c.internal_key.hex(),
            "2098b16fde9f60657ddaa1a777ecbbf40eb579612f84d358831b14dab49b06ac",
        )
        self.assertEqual(
            c.merkle_root.hex(),
            "5811bfb9b566b41985bb3f96b59fe5c53d3d14cbd787bcc08285d0c8ac50ac98",
        )
        self.assertEqual(
            c.control_block(c.BUYER_COORDINATOR).hex(),
            "c02098b16fde9f60657ddaa1a777ecbbf40eb579612f84d358831b14dab49b06ac"
            "aa243db7da084bdacd0df00cd80bce8787925afe68aefc28914b22035b3695478c"
            "4ada3f02f152ad750361267e254b94e0388199eb1e6656702d2348d569f518",
        )

    def test_internal_key_is_seller_and_buyer_only(self):
        c = _contract()
        self.assertEqual(c.internal_key, M.get_xonly_pk(M.key_agg([bytes.fromhex(S), bytes.fromhex(B)])))

    def test_leaves_have_no_op_if(self):
        for leaf in _contract().leaves:
            ops, i = [], 0
            while i < len(leaf):
                op = leaf[i]
                i += 1 + (op if 0 < op < 0x4C else 0)
                ops.append(op)
            self.assertFalse({0x63, 0x64} & set(ops))

    def test_regtest_hrp(self):
        c = EscrowContract(bytes.fromhex(S), bytes.fromhex(B), bytes.fromhex(C), 20, hrp="bcrt")
        self.assertTrue(c.address.startswith("bcrt1p"))
        self.assertEqual(p2tr_address(c.output_key, "bcrt"), c.address)

    def test_refuses_repeated_keys_and_bad_csv(self):
        with self.assertRaises(ValueError):
            EscrowContract(bytes.fromhex(S), bytes.fromhex(S), bytes.fromhex(C), 10)
        with self.assertRaises(ValueError):
            EscrowContract(bytes.fromhex(S), bytes.fromhex(B), bytes.fromhex(C), 0)
        with self.assertRaises(ValueError):
            EscrowContract(bytes.fromhex(S), bytes.fromhex(B), bytes.fromhex(C), 70000)


class UnifiedSighashTest(SimpleTestCase):
    def test_keypath_vector(self):
        c = _contract()
        msg = _tx().sighash_unified(0, [TxOut(100_000, c.script_pubkey)])
        self.assertEqual(
            msg.hex(), "3171975a2f622d452db5a19239652d22d3142aeef865fd54c645f3bbc51a993c"
        )

    def test_leaf_vector(self):
        c = _contract()
        msg = _tx().sighash_unified(
            0,
            [TxOut(100_000, c.script_pubkey)],
            SIGHASH_ALL_UNIFIED,
            c.leaf_hash(c.BUYER_COORDINATOR),
        )
        self.assertEqual(
            msg.hex(), "9c3276df71449904047e7525dec7f1b2a6c437339089f639c8620dc3413a9d65"
        )

    def test_commits_to_amount_and_leaf(self):
        c = _contract()
        tx = _tx()
        base = tx.sighash_unified(0, [TxOut(100_000, c.script_pubkey)])
        self.assertNotEqual(base, tx.sighash_unified(0, [TxOut(100_001, c.script_pubkey)]))
        leaf1 = tx.sighash_unified(0, [TxOut(100_000, c.script_pubkey)], SIGHASH_ALL_UNIFIED, c.leaf_hash(0))
        leaf2 = tx.sighash_unified(0, [TxOut(100_000, c.script_pubkey)], SIGHASH_ALL_UNIFIED, c.leaf_hash(1))
        self.assertNotEqual(leaf1, leaf2)

    def test_only_all_unified(self):
        c = _contract()
        with self.assertRaises(ValueError):
            _tx().sighash_unified(0, [TxOut(100_000, c.script_pubkey)], 0x01)


class SigningTest(SimpleTestCase):
    def test_musig_keypath_signature_verifies_for_output_key(self):
        sk_s, sk_b, _ = _keys()
        c = _contract()
        msg = _tx().sighash_unified(0, [TxOut(100_000, c.script_pubkey)])
        pks, tweaks, isx = c.musig_pubkeys(), [c.tweak], [True]
        sec_s, pub_s = M.nonce_gen(sk_s, pks[0], c.output_key, msg, None)
        sec_b, pub_b = M.nonce_gen(sk_b, pks[1], c.output_key, msg, None)
        ctx = M.SessionContext(M.nonce_agg([pub_s, pub_b]), pks, tweaks, isx, msg)
        sig = M.partial_sig_agg([M.sign(sec_s, sk_s, ctx), M.sign(sec_b, sk_b, ctx)], ctx)
        self.assertTrue(schnorr_verify(msg, c.output_key, sig))
        self.assertEqual(keypath_witness(sig)[0][-1], 0x21)

    def test_coordinator_signature_is_bip340(self):
        _, _, sk_c = _keys()
        msg = bytes(32)
        sig = schnorr_sign(sk_c, msg)
        self.assertTrue(schnorr_verify(msg, xonly(pubkey(sk_c)), sig))

    def test_leaf_witness_layout(self):
        c = _contract()
        w = leaf_witness(c, c.BUYER_COORDINATOR, [b"\x01" * 64, b"\x02" * 64])
        self.assertEqual([len(x) for x in w[:2]], [65, 65])
        self.assertEqual(w[2], c.leaves[c.BUYER_COORDINATOR])
        self.assertEqual(w[3], c.control_block(c.BUYER_COORDINATOR))

    def test_txid_and_vsize(self):
        tx = _tx()
        self.assertEqual(len(tx.txid()), 64)
        tx.inputs[0].witness = [b"\x00" * 65]
        self.assertEqual(tx.vsize(), 112)
