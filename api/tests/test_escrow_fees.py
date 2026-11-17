"""Onchain coordinator fee floor and dev share (rl31-beta13).

Orders #20/#21 on Kilobot counted ~150 sats of proceeds that never reached the coordinator:
the fee output was below dust and the deposit planner dropped it (the sats went to miners)."""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from api.escrow import flow
from api.escrow.transactions import Utxo, plan_deposit
from api.tests.test_escrow_transactions import SPK, _setup


class FeeFloor(SimpleTestCase):
    def _fees(self, split, at_release=False, floor="1000"):
        with patch("api.escrow.service.fee_split", return_value=split), \
             patch("api.escrow.flow.buyer_fee_at_release", return_value=at_release), \
             patch.dict("os.environ", {"ESCROW_MIN_COORDINATOR_FEE_SATS": floor}):
            return flow.fees(object())

    def test_small_fees_are_raised_to_the_floor_in_proportion(self):
        s, b = self._fees((17, 136))
        self.assertEqual(s + b, 1000)
        self.assertGreaterEqual(b, s)

    def test_big_fees_untouched(self):
        self.assertEqual(self._fees((1700, 200)), (1700, 200))

    def test_hybrid_one_floor_for_the_order(self):  # rl31-beta15 (fee plan 2)
        self.assertEqual(sum(self._fees((17, 136), at_release=True)), 1000)

    def test_floor_off(self):
        self.assertEqual(self._fees((170, 1360), floor="0"), (170, 1360))

    def test_dev_off_by_default_and_never_starves_the_coordinator(self):
        self.assertEqual(flow.dev_fee(None, 5000), (0, None))
        with patch.dict("os.environ", {"ESCROW_DEV_FEE_PERCENT": "10",
                                       "ESCROW_DEV_ADDRESS": "bcrt1q7sgsct6fvdzd684hnnmwnawud3d9yp0ugypjyr"}):
            sats, spk = flow.dev_fee(None, 5000)
            self.assertEqual(sats, 500)
            self.assertIsNotNone(spk)
            self.assertEqual(flow.dev_fee(None, 1200), (0, None))  # 1200-120 < 1000

    def test_deposit_pays_the_raised_fee_as_an_output(self):
        c, fs = _setup()
        us = [Utxo("aa" * 32, 0, 120_000, fs)]
        ub = [Utxo("bb" * 32, 1, 3_000, SPK(9))]
        plan = plan_deposit(us, ub, c, 100_000, 860, 140, SPK(1), 0, None, fs, SPK(9), 2.0)
        coord = [o.value for o in plan.tx.outputs if o.script_pubkey == SPK(1)]
        self.assertEqual(coord, [1000])
