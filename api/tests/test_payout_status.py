"""Roblosats 0.9.7: the final summary says whether an onchain payout (swap, hybrid B or escrow
release) is in the mempool or confirmed, from the coordinator's node."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from api import order_summary
from api.escrow import service


class TxStatus(TestCase):
    def setUp(self):
        cache.clear()

    def test_confirmed_and_mempool(self):
        with patch.object(service, "rpc", return_value={"confirmations": 3}):
            self.assertEqual(order_summary.tx_status("aa" * 32), "confirmed")
        with patch.object(service, "rpc", return_value={"confirmations": 0}):
            self.assertEqual(order_summary.tx_status("bb" * 32), "mempool")

    def test_node_down_or_unknown_tx(self):
        with patch.object(service, "rpc", side_effect=service.EscrowError("no such tx")):
            self.assertIsNone(order_summary.tx_status("cc" * 32))
        self.assertIsNone(order_summary.tx_status(""))

    def test_cached(self):
        with patch.object(service, "rpc", return_value={"confirmations": 0}) as rpc:
            order_summary.tx_status("dd" * 32)
            order_summary.tx_status("dd" * 32)
        self.assertEqual(rpc.call_count, 1)
