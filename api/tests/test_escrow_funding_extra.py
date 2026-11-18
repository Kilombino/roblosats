"""Roblosats rl31-beta20: no small funding leftover is stranded.

Kilobot #22 (18 nov 2026): the seller's funding address kept 331 sats after the deposit (too
small for a change output, and later too small to sweep). Now the deposit spends every
confirmed coin of the funding address; an excess too small for a change output of its own
rides in the escrow output and goes back to its owner with its bond, in the release, the
collaborative cancel, both dispute releases and the pre-signed rescue (exact outputs checked
on chain, as the parent class does for the normal case). Escrows created before keep the old
rule (no "extra_plan").
"""

import math
from unittest import skipUnless
from unittest.mock import patch

from api.escrow import service
from api.tests import test_escrow_service_regtest as base
from api.escrow.transactions import VB_BOND_COOP, VB_OVERHEAD, VB_TAPROOT_KEY_INPUT, output_vbytes
from api.tests.test_escrow_service_regtest import REGTEST

RATE = 5.0


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class FundingExtraRegtest(base.EscrowOrderFlowRegtest):
    funding_extra = {"seller": 331, "buyer": 400}
    test_taker_never_funds_is_penalized = None
    test_rescue_plan_1_kept_for_old_escrows = None

    def setUp(self):
        super().setUp()
        # a fixed fee rate, so the test knows to the sat what the deposit needs from each one
        p = patch.object(service, "fee_rate", return_value=RATE)
        p.start()
        self.addCleanup(p.stop)

    def _funding_amount(self, d, role):
        """Exactly what the deposit needs from this trader with no change output, plus
        funding_extra: too small for a change output of its own (dust + 43 vB at RATE)."""
        shared = VB_OVERHEAD + output_vbytes(b"\x51\x20" + b"\x00" * 32)  # plan 2: only the escrow output
        fee = math.ceil((VB_TAPROOT_KEY_INPUT + VB_BOND_COOP + shared / 2) * RATE)
        return d["escrow_funding_owed"] + fee + self.funding_extra[role]

    def _to_chat(self):
        from api.escrow import flow

        out = super()._to_chat()
        order, buyer, seller, _ = out
        escrow = flow.escrow_of(order)
        self.assertEqual(service.extra_plan(escrow), 1)
        # what each sent beyond the recommended amount (plus any rounding) is in the escrow
        self.assertEqual(self.extra["seller"], 331)
        if escrow.funding_signatures["plan"]["buyer_inputs"]:
            self.assertEqual(self.extra["buyer"], 400)
        else:  # the buyer's fee came out of its bond: it funded nothing
            self.assertEqual(self.extra["buyer"], 0)
        deposit = service.rpc("getrawtransaction", [escrow.funding_txid, True])
        funding = {flow._funding_address(escrow, r) for r in service.ROLES}
        # no change back to a funding address and nothing left there
        self.assertFalse([o for o in deposit["vout"] if o["scriptPubKey"].get("address") in funding])
        self.assertEqual(service.funding_utxos(escrow, "seller"), [])
        # both apps are told (to check the release/rescue and to show it)
        d = flow.order_data(order, seller)
        self.assertEqual(d["escrow_extra_in_escrow"], self.extra)
        self.assertEqual(d["escrow_output_sats"], escrow.escrow_sats)
        return out

    def test_summary_counts_the_extra_back(self):
        from api.logics import Logics
        from api.order_summary import final_summary

        order, buyer, seller, keys = self._to_chat()
        Logics.confirm_fiat(order, buyer)
        order.refresh_from_db()
        Logics.confirm_fiat(order, seller)
        order.refresh_from_db()
        escrow = self._musig(order, {"seller": seller, "buyer": buyer}, keys)
        self._release_outputs(escrow)
        order.refresh_from_db()
        s = final_summary(order, seller)
        self.assertEqual((s["escrow_extra_sats"], s["escrow_extra_returned_sats"]), (self.extra["seller"],) * 2)
        self.assertEqual(s["bond_returned_sats"], 30_000)
        b = final_summary(order, buyer)
        self.assertEqual(b["escrow_extra_returned_sats"], self.extra["buyer"])
        self.assertEqual(b["received_sats"], 1_000_000 - (escrow.escrow_sats - sum(
            round(o["value"] * 1e8) for o in service.rpc("getrawtransaction", [escrow.release_txid, True])["vout"])))
