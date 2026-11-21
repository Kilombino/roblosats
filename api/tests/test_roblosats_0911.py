"""0.9.11: the automatic bond return's fee ladder (pure logic; the regtest is
EscrowAutoReturnLadderRegtest)."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from api.escrow import bonds


class LadderRates(SimpleTestCase):
    def cfg(self, value):
        return patch("api.escrow.bonds.service.config", side_effect=lambda k, cast=None, default=None: value
                     if k == "ESCROW_AUTO_RETURN_RATES" else default)

    def test_default_rates_below_the_top(self):
        with self.cfg("1,3"):
            self.assertEqual(bonds.ladder_rates(10), [1.0, 3.0])
            self.assertEqual(bonds.ladder_rates(2.5), [1.0])
            self.assertEqual(bonds.ladder_rates(1.0), [])

    def test_configured_rates_cleaned(self):
        with self.cfg(" 5, x, 2,2, 0.5, 12"):
            self.assertEqual(bonds.ladder_rates(10), [2.0, 5.0])

    def test_choose_rung(self):
        rungs = [(0, 1.0, "a", ["s"]), (1, 3.0, "b", ["s"]), (2, 10.0, "c", ["s"])]
        self.assertEqual(bonds.choose_rung(rungs, 1.0)[1], 1.0)
        self.assertEqual(bonds.choose_rung(rungs, 1.2)[1], 3.0)
        self.assertEqual(bonds.choose_rung(rungs, 25)[1], 10.0)
        self.assertIsNone(bonds.choose_rung([], 1))

    def test_signed_rungs_top_only_and_full(self):
        ar = {"tx": "c", "rate": 10.0, "sigs": ["top"],
              "ladder": [{"rate": 1.0, "tx": "a"}, {"rate": 3.0, "tx": "b"}, {"rate": 10.0, "tx": "c"}]}
        bond = SimpleNamespace(auto_return=ar)
        self.assertEqual([r[1] for r in bonds.signed_rungs(bond)], [10.0])  # an old app
        ar["ladder_sigs"] = [["a1"], ["b1"], ["top"]]
        self.assertEqual([(r[1], r[3]) for r in bonds.signed_rungs(bond)], [(1.0, ["a1"]), (3.0, ["b1"]), (10.0, ["top"])])
        # plan 1: no ladder, the single return
        bond = SimpleNamespace(auto_return={"tx": "c", "rate": 10.0, "sigs": ["top"]})
        self.assertEqual([r[1] for r in bonds.signed_rungs(bond)], [10.0])


class Bump(SimpleTestCase):
    def bond(self, sent_rate=1.0, at=1000):
        from api.models import OnchainBond

        ar = {"tx": "c", "rate": 10.0, "sigs": ["t"], "spk": "00",
              "ladder": [{"rate": 1.0, "tx": "a"}, {"rate": 3.0, "tx": "b"}, {"rate": 10.0, "tx": "c"}],
              "ladder_sigs": [["a"], ["b"], ["t"]], "sent": {"index": 0, "rate": sent_rate, "txid": "aa", "at": at}}
        b = SimpleNamespace(status=OnchainBond.Status.RETURNED, auto_return=ar, spend_txid="aa", role="maker",
                            order=SimpleNamespace(log=lambda *a, **k: None), save=lambda **k: None)
        return b

    def run_bump(self, bond, now, status="pending", rate=1.0):
        sent = SimpleNamespace(txid=lambda: "bb")
        with patch("api.escrow.bonds.tx_status", return_value=status), \
                patch("api.escrow.bonds.target_rate", return_value=rate), \
                patch("api.escrow.bonds._send_rung", return_value=sent) as send:
            return bonds.bump_auto_return(bond, now=now), send

    def test_waits_then_bumps_to_the_next_rung(self):
        b = self.bond()
        ok, send = self.run_bump(b, now=1000 + 60)
        self.assertFalse(ok)
        send.assert_not_called()
        ok, send = self.run_bump(b, now=1000 + 7300)
        self.assertTrue(ok)
        self.assertEqual(send.call_args.args[1][1], 3.0)
        self.assertEqual(b.auto_return["sent"]["rate"], 3.0)
        self.assertEqual(b.spend_txid, "bb")

    def test_bumps_at_once_when_the_estimate_rises(self):
        ok, send = self.run_bump(self.bond(), now=1060, rate=2.0)
        self.assertTrue(ok)

    def test_confirmed_or_top_stops(self):
        b = self.bond()
        ok, _ = self.run_bump(b, now=99999, status="confirmed")
        self.assertFalse(ok)
        self.assertTrue(b.auto_return["confirmed"])
        ok, _ = self.run_bump(self.bond(sent_rate=10.0), now=99999)
        self.assertFalse(ok)
