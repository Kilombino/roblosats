"""Roblosats 0.9.7: the automatic return of an onchain bond, pre-signed by its owner.

Safety rules: it is broadcast only from release() (never while the bond can be penalized or
is in a deposit), it pays only the owner's return address, and the address cannot be swapped
once signed. Everything else falls back to the manual return."""

import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api.escrow import bonds, service
from api.escrow.taproot import derive_secret, pubkey, schnorr_sign, with_hashtype
from api.models import OnchainBond, Order

COORD = derive_secret(b"coordinador", "kilobot", 7, "coordinator")
OWNER = derive_secret(b"tok-owner", "kilobot", 7, "bond")
RETURN_SPK = bytes.fromhex("5120" + "ab" * 32)
OTHER_SPK = bytes.fromhex("0014" + "cd" * 20)


class AutoReturn(TestCase):
    def setUp(self):
        u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
        now = timezone.now()
        self.order = Order.objects.create(
            type=Order.Types.BUY, status=Order.Status.PUB, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100,
            satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000, maker=u, bond_size=3,
        )
        self.p = [patch.object(bonds, "coordinator_bond_key", return_value=COORD),
                  patch.object(service, "network_hrp", return_value="bc")]
        for p in self.p:
            p.start()
        self.bond = OnchainBond.objects.create(
            order=self.order, owner=u, role="maker", owner_pubkey=pubkey(OWNER).hex(),
            coordinator_pubkey=pubkey(COORD).hex(), refund_csv=540, address="x", script_pubkey="",
            required_sats=30_000, status=OnchainBond.Status.SIGN, txid="11" * 32, vout=0, value=30_000,
            coins=[["11" * 32, 0, 30_000]],
        )
        self.sent = []

    def tearDown(self):
        for p in self.p:
            p.stop()

    def rpc(self, method, params=None, wallet=None):
        if method == "sendrawtransaction":
            self.sent.append(params[0])
            return "ok"
        raise AssertionError(method)

    def signed(self):
        bonds.set_return_address(self.bond, RETURN_SPK, "bc1preturn")
        self.bond.refresh_from_db()
        _, msgs = bonds.auto_return_message(self.bond)
        bonds.submit_auto_return_signature(self.bond, [with_hashtype(schnorr_sign(OWNER, m)).hex() for m in msgs])
        self.bond.refresh_from_db()

    def test_built_and_signed_to_the_return_address_only(self):
        self.signed()
        tx, _ = bonds.auto_return_message(self.bond)
        self.assertEqual(len(tx.outputs), 1)
        self.assertEqual(tx.outputs[0].script_pubkey, RETURN_SPK)
        self.assertGreater(tx.outputs[0].value, 27_000)
        self.assertTrue(bonds.bond_data(self.bond)["bond_auto_return_signed"])

    def test_address_cannot_change_once_signed(self):
        self.signed()
        with self.assertRaises(service.EscrowError):
            bonds.set_return_address(self.bond, OTHER_SPK, "bc1qother")
        bonds.set_return_address(self.bond, RETURN_SPK, "bc1preturn")  # the same: fine

    def test_not_broadcast_while_held_penalized_or_in_deposit(self):
        self.signed()
        for status in (OnchainBond.Status.LOCKED, OnchainBond.Status.PENALIZED, OnchainBond.Status.IN_DEPOSIT):
            self.bond.status = status
            self.bond.save()
            with patch.object(service, "rpc", side_effect=self.rpc):
                self.assertFalse(bonds._broadcast_auto_return(self.bond))
        self.assertEqual(self.sent, [])
        # in a deposit or penalized, release() does not free it at all
        for status in (OnchainBond.Status.PENALIZED, OnchainBond.Status.IN_DEPOSIT):
            self.bond.status = status
            self.bond.save()
            with patch.object(service, "rpc", side_effect=self.rpc):
                self.assertFalse(bonds.release(self.bond))
        self.assertEqual(self.sent, [])

    def test_release_of_a_held_bond_sends_it(self):
        self.signed()
        self.bond.status = OnchainBond.Status.LOCKED
        self.bond.save()
        with patch.object(service, "rpc", side_effect=self.rpc):
            self.assertTrue(bonds.release(self.bond))
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNED)
        self.assertEqual(len(self.sent), 1)

    def test_a_tampered_return_is_refused_and_falls_back(self):
        self.signed()
        ar = dict(self.bond.auto_return)
        ar["spk"] = OTHER_SPK.hex()  # someone changes where it pays
        self.bond.auto_return = ar
        self.bond.status = OnchainBond.Status.LOCKED
        self.bond.save()
        with patch.object(service, "rpc", side_effect=self.rpc):
            bonds.release(self.bond)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)  # manual return
        self.assertEqual(self.sent, [])

    def test_bad_signature_refused(self):
        bonds.set_return_address(self.bond, RETURN_SPK, "bc1preturn")
        self.bond.refresh_from_db()
        _, msgs = bonds.auto_return_message(self.bond)
        with self.assertRaises(service.EscrowError):
            bonds.submit_auto_return_signature(self.bond, [with_hashtype(schnorr_sign(COORD, m)).hex() for m in msgs])

    def test_old_bonds_without_plan(self):
        self.bond.status = OnchainBond.Status.LOCKED
        self.bond.save()
        with patch.object(service, "rpc", side_effect=self.rpc):
            bonds.release(self.bond)
        self.bond.refresh_from_db()
        self.assertEqual(self.bond.status, OnchainBond.Status.RETURNABLE)
        self.assertNotIn("bond_auto_return_address", bonds.bond_data(self.bond))
