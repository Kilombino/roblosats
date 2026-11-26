"""Payout invoices must be Bitcoin BLAKE2b ones (feature 512/513), not Spamcoin (26 nov 2026)."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from api.lightning.lnd import LNDNode


def decoded(features):
    return SimpleNamespace(
        route_hints=[], features={f: None for f in features}, num_satoshis=1000,
        created_at=4102444800, expiry=3600, description="", payment_hash="00" * 32,
    )


class Blake2bInvoiceTests(SimpleTestCase):
    def check(self, features):
        with patch.object(LNDNode, "decode_payreq", return_value=decoded(features)):
            return LNDNode.validate_ln_invoice("lnbc...", 1000, 0)

    def test_spamcoin_invoice_is_refused(self):
        r = self.check([8, 14, 17])
        self.assertFalse(r["valid"])
        self.assertIn("BLAKE2b", r["context"]["bad_invoice"])

    def test_blake2b_invoice_passes_the_check(self):
        for bit in (512, 513):
            r = self.check([8, 14, 17, bit])
            self.assertNotIn("BLAKE2b", str((r["context"] or {}).get("bad_invoice", "")))
