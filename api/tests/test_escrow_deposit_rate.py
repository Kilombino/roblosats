"""0.9.3 (Kilobot #24): a trader that sent the recommended funding is never asked to pay
again because the deposit's live fee rate came out a few sats above it: the deposit pays the
highest rate the funding covers (down to ESCROW_MIN_DEPOSIT_FEE_RATE)."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from api.escrow import service
from api.escrow.transactions import DepositError


def _fake_plan(limit):
    calls = []

    def plan(*args, **kwargs):
        rate = args[11]
        calls.append(rate)
        if rate > limit:
            raise DepositError(f"the seller's funding is short by {int((rate - limit) * 200)} sats")
        out = SimpleNamespace(value=1_030_200, script_pubkey=b"\x51")
        tx = Mock(outputs=[out])
        tx.serialize.return_value = b"\x00"
        return SimpleNamespace(
            tx=tx, spent=[], seller_inputs=[0], buyer_inputs=[], bond_inputs={}, bond_input_lists={},
            bond_in_escrow={}, seller_pays=0, buyer_pays=0, seller_mining=int(rate * 190), buyer_mining=0,
            mining_fee=int(rate * 190), extra_in_escrow={}, rate=rate,
        )

    return plan, calls


class DepositRate(SimpleTestCase):
    def _build(self, live_rate, limit, floor="1.0"):
        plan, calls = _fake_plan(limit)
        escrow = Mock(order_id=24, funding_signatures={"extra_plan": 1})
        with patch.object(service, "funding_utxos", return_value=[Mock(value=1_000_889)]), \
                patch.object(service, "contract_for"), patch.object(service, "funding_spk", return_value=b"\x51"), \
                patch.object(service, "fee_rate", return_value=live_rate), \
                patch.object(service, "plan_deposit", side_effect=plan), \
                patch("api.escrow.service.config", side_effect=lambda k, cast=None, default=None: cast(floor) if k == "ESCROW_MIN_DEPOSIT_FEE_RATE" else default):
            result = service.build_deposit(escrow, 1_000_000, 200, 0, b"\x52", 0, None, {}, fees_in_escrow=True)
        return result, calls, escrow

    def test_live_rate_used_when_the_funding_covers_it(self):
        result, calls, escrow = self._build(3.0, 5.0)
        self.assertEqual(calls, [3.0])
        self.assertEqual(result.rate, 3.0)

    def test_short_funding_pays_the_highest_rate_it_covers(self):
        result, calls, escrow = self._build(3.647, 3.57)
        self.assertLessEqual(result.rate, 3.57)
        self.assertGreater(result.rate, 3.56)
        self.assertEqual(escrow.funding_signatures["plan"]["seller_mining"], int(result.rate * 190))

    def test_still_short_at_the_floor_keeps_the_error(self):
        with self.assertRaisesRegex(DepositError, "short by"):
            self._build(3.647, 0.5)

    def test_other_errors_are_not_retried(self):
        escrow = Mock(order_id=24, funding_signatures={})
        with patch.object(service, "funding_utxos", return_value=[]), patch.object(service, "contract_for"), \
                patch.object(service, "funding_spk", return_value=b"\x51"), patch.object(service, "fee_rate", return_value=3.0), \
                patch.object(service, "plan_deposit", side_effect=DepositError("the seller has not funded yet")) as p:
            with self.assertRaisesRegex(DepositError, "not funded"):
                service.build_deposit(escrow, 1_000_000, 200, 0, b"\x52", 0, None, {})
        self.assertEqual(p.call_count, 1)
