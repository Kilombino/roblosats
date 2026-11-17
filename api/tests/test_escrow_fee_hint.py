"""Roblosats rl31-beta19: fee hint on the onchain payment screens and the low-fee warning.

- fee_hint(): what a payment from the trader's own wallet should pay (next block / about an
  hour), from the coordinator's node (estimatesmartfee), else from the mempool instance of this
  chain, else nothing (the app hides the hint);
- tx_fee_rate(): what a payment seen in the mempool pays (getmempoolentry, counting parents and
  CPFP children); below the rate for about an hour the app and the Telegram notice warn.

Unit tests run anywhere; the regtest one needs the lab (ESCROW_REGTEST=1).
"""

from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase

from api.escrow import flow
from api.escrow.service import EscrowError
from api.tests import test_escrow_bond_topup as topup
from api.tests import test_escrow_service_regtest as base
from api.tests.test_escrow_service_regtest import REGTEST, _faucet, _mine


class FakeCache:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v, timeout=None):
        self.d[k] = v


def node(estimates, minfee=0.000001, fail=None):
    def rpc(method, params=None, wallet=None):
        if fail and method in fail:
            raise EscrowError(f"bitcoind {method}: down")
        if method == "getmempoolinfo":
            return {"mempoolminfee": minfee, "minrelaytxfee": 0.000001}
        if method == "estimatesmartfee":
            rate = estimates.get(params[0])
            return {"feerate": rate, "blocks": params[0]} if rate else {"errors": ["Insufficient data"], "blocks": 0}
        raise AssertionError(method)

    return rpc


class FeeHint(SimpleTestCase):
    def setUp(self):
        self.cache = FakeCache()
        p = patch("django.core.cache.cache", self.cache)
        p.start()
        self.addCleanup(p.stop)

    def test_from_the_node(self):
        with patch.object(flow.service, "rpc", node({2: 0.0000234, 6: 0.000005})):
            self.assertEqual(flow.fee_hint(), {"next_block": 2.4, "hour": 0.5})

    def test_never_below_the_mempool_minimum(self):
        with patch.object(flow.service, "rpc", node({2: 0.000002, 6: 0.000001}, minfee=0.000015)):
            self.assertEqual(flow.fee_hint(), {"next_block": 1.5, "hour": 1.5})

    def test_hour_never_above_next_block(self):
        with patch.object(flow.service, "rpc", node({2: 0.0001, 6: 0.0003})):
            self.assertEqual(flow.fee_hint(), {"next_block": 10.0, "hour": 10.0})

    def test_mempool_when_the_node_has_no_estimate(self):
        with patch.object(flow.service, "rpc", node({})), patch(
            "api.mempool._mempool_fees_with_hard_timeout", return_value={"fastestFee": 4, "hourFee": 1.2}
        ):
            self.assertEqual(flow.fee_hint(), {"next_block": 4.0, "hour": 1.2})

    def test_nothing_when_neither_answers_and_the_failure_is_cached(self):
        rpc = node({}, fail={"estimatesmartfee", "getmempoolinfo"})
        with patch.object(flow.service, "rpc", rpc) as r, patch(
            "api.mempool._mempool_fees_with_hard_timeout", side_effect=TimeoutError()
        ) as m:
            self.assertIsNone(flow.fee_hint())
            self.assertIsNone(flow.fee_hint())
            self.assertEqual(m.call_count, 1)
        self.assertEqual(self.cache.get(flow.FEE_HINT_CACHE), {})

    def test_cached(self):
        with patch.object(flow.service, "rpc", node({2: 0.00002, 6: 0.00001})):
            flow.fee_hint()
        with patch.object(flow.service, "rpc", side_effect=AssertionError("cached")):
            self.assertEqual(flow.fee_hint(), {"next_block": 2.0, "hour": 1.0})


def entry(base, vsize, anc=None, desc=None):
    e = {"vsize": vsize, "fees": {"base": base, "ancestor": base, "descendant": base},
         "ancestorsize": vsize, "descendantsize": vsize}
    if anc:
        e["fees"]["ancestor"], e["ancestorsize"] = anc
    if desc:
        e["fees"]["descendant"], e["descendantsize"] = desc
    return e


class TxFeeRate(SimpleTestCase):
    def rate(self, e):
        with patch.object(flow.service, "rpc", return_value=e):
            return flow.tx_fee_rate("aa" * 32)

    def test_own_rate(self):
        self.assertEqual(self.rate(entry(0.00000141, 141)), 1.0)
        self.assertEqual(self.rate(entry(0.00000035, 141)), 0.2)  # 0.248 rounds down

    def test_parent_drags_it_down(self):
        self.assertEqual(self.rate(entry(0.00000500, 100, anc=(0.00000600, 300))), 2.0)

    def test_child_bumps_it(self):
        self.assertEqual(self.rate(entry(0.00000100, 100, desc=(0.00002100, 300))), 7.0)

    def test_not_in_the_mempool(self):
        with patch.object(flow.service, "rpc", side_effect=EscrowError("bitcoind getmempoolentry: not in mempool")):
            self.assertIsNone(flow.tx_fee_rate("aa" * 32))

    def test_low_fee(self):
        hint = {"next_block": 5.0, "hour": 2.0}
        self.assertEqual(flow.low_fee(1.0, hint), 2.0)
        self.assertIsNone(flow.low_fee(2.0, hint))
        self.assertIsNone(flow.low_fee(None, hint))
        with patch.object(flow, "fee_hint", return_value=None):
            self.assertIsNone(flow.low_fee(1.0))


class MempoolNotice(SimpleTestCase):
    HINT = {"next_block": 5.0, "hour": 2.5}

    def test_message(self):
        item = {"owner": 7, "what": "bond", "sats": 3330, "short": 0, "fee_rate": 1.0}
        self.assertEqual(flow.mempool_message(item, self.HINT), "onchain_mempool:7:bond:3330:0:1:2.5")
        item["fee_rate"] = 3.0
        self.assertEqual(flow.mempool_message(item, self.HINT), "onchain_mempool:7:bond:3330:0")
        funding = {"owner": 7, "what": "funding", "sats": 50000, "fee_rate": 0.5}
        self.assertEqual(flow.mempool_message(funding, self.HINT), "onchain_mempool:7:funding:50000::0.5:2.5")
        funding["fee_rate"] = None
        self.assertEqual(flow.mempool_message(funding, self.HINT), "onchain_mempool:7:funding:50000")

    def _sent(self, message, lang):
        from api import tasks
        from api.notifications import Notifications

        class Robot:
            telegram_lang_code = lang

        class User:
            id = 7
            robot = Robot()

        class Order:
            id = 22
            maker_id = 7
            maker = User()
            taker = None

        with patch("api.models.Order.objects.get", return_value=Order()), patch.object(
            Notifications, "send_message"
        ) as send:
            tasks.send_notification.run(order_id=22, message=message)
        return send.call_args[0][2]

    def test_telegram_warns_when_it_pays_too_little(self):
        text = self._sent("onchain_mempool:7:funding:50000::0.5:2.5", "en")
        self.assertIn("Your payment pays 0.5 sat/vB and the mempool currently needs 2.5", text)
        self.assertIn("RBF or CPFP", text)
        text = self._sent("onchain_mempool:7:bond:3330:0:1:2.5", "es")
        self.assertIn("llega a la fianza", text)
        self.assertIn("Tu pago paga 1 sat/vB y ahora la mempool pide 2,5", text)

    def test_old_messages_still_work(self):
        self.assertNotIn("sat/vB", self._sent("onchain_mempool:7:bond:3330:30", "en"))
        self.assertIn("30 Sats missing", self._sent("onchain_mempool:7:bond:3330:30", "en"))
        self.assertNotIn("sat/vB", self._sent("onchain_mempool:7:funding:50000", "en"))


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class FeeHintRegtest(TransactionTestCase):
    """Against the lab node: a bond payment at 1.2 sat/vB, the hint, the rate the order page
    shows and the Telegram notice; then a CPFP child lifts it above the hour rate. Borrows the
    lab helpers without re-running the flow tests of the classes they belong to."""

    setUp = base.EscrowOrderFlowRegtest.setUp
    tearDown = base.EscrowOrderFlowRegtest.tearDown
    _keys = base.EscrowOrderFlowRegtest._keys
    _new_order = topup.BondTopUpRegtest._new_order
    _open = topup.BondTopUpRegtest._open

    def test_low_fee_bond_payment(self):
        from api.escrow import service

        order, buyer, _ = self._new_order()
        kb = self._keys(order, "buyer")
        hint = {"next_block": 5.0, "hour": 3.0}
        with patch.object(flow, "fee_hint", return_value=hint):
            d = self._open(order, buyer, kb)
            self.assertEqual(d["onchain_fee_hint"], hint)
            mine = _faucet("getnewaddress")
            txid = _faucet("sendmany", "", {d["bond_address"]: 30_000 / 1e8, mine: 100_000 / 1e8}, 1, "", [], True,
                           None, "unset", 1.2)
            d = flow.order_data(order, buyer)
            self.assertEqual(d["bond_mempool_txid"], txid)
            self.assertAlmostEqual(d["bond_mempool_fee_rate"], 1.2, delta=0.1)
            seen = [s for s in flow.mempool_seen(order) if s["what"] == "bond"]
            msg = flow.mempool_message(seen[0], hint)
            self.assertTrue(msg.startswith(f"onchain_mempool:{buyer.id}:bond:30000:0:1."), msg)
            self.assertTrue(msg.endswith(":3"), msg)
            # CPFP: the faucet spends its change at a high rate and lifts the payment
            tx = service.rpc("getrawtransaction", [txid, True])
            out = next(o for o in tx["vout"] if o["scriptPubKey"].get("address") == mine)
            child = service.rpc("createrawtransaction", [[{"txid": txid, "vout": out["n"]}],
                                                         [{_faucet("getnewaddress"): round(out["value"] - 0.0005, 8)}]])
            signed = _faucet("signrawtransactionwithwallet", child)
            service.rpc("sendrawtransaction", [signed["hex"]])
            d = flow.order_data(order, buyer)
            self.assertGreater(d["bond_mempool_fee_rate"], 3.0)
            self.assertNotIn(":3", flow.mempool_message(
                [s for s in flow.mempool_seen(order) if s["what"] == "bond"][0], hint)[-2:])
        _mine()
        # the real hint on the lab node: a dict or nothing, never an error
        self.assertTrue(flow.fee_hint() is None or set(flow.fee_hint()) == {"next_block", "hour"})
