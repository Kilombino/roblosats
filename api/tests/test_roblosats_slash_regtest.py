"""rl31-beta22 against the regtest lab: slashed onchain bonds pay the innocent trader the reward
share of what the penalty really paid, nothing goes back to the trader at fault, proceeds are
what reached the coordinator on chain minus the reward, and a minimum bond's penalty pays at
most 10 % in mining. Opt-in (ESCROW_REGTEST=1), see test_escrow_service_regtest.py."""

import math
from unittest import skipUnless
from unittest.mock import patch

from django.utils import timezone

from api.escrow import service
from api.escrow.taproot import pubkey
from api.models import Order
from api.tests.test_escrow_service_regtest import REGTEST, EscrowHybridRegtest, _faucet, _mine, _user


@skipUnless(REGTEST, "needs the regtest lab (ESCROW_REGTEST=1)")
class SlashRegtest(EscrowHybridRegtest):
    test_case_a_lightning_buyer_to_successful = None
    test_case_a_small_order_one_minimum_in_total = None
    test_case_b_proxy_return_and_penalty_onchain = None

    def _coordinator_got(self, txid):
        """Sats the coordinator's address receives in this (confirmed) transaction."""
        from api.escrow import flow

        addr = service.rpc("decodescript", [flow.coordinator_fee_spk().hex()])["address"]
        info = service.rpc("getrawtransaction", [txid, True])
        self.assertGreaterEqual(info["confirmations"], 1)
        return sum(round(o["value"] * 1e8) for o in info["vout"] if o["scriptPubKey"].get("address") == addr)

    def _rewards(self, user):
        user.robot.refresh_from_db()
        return user.robot.earned_rewards

    def test_order_23_replayed(self):
        """Hybrid B, 50,000 sats: the maker (buyer) bond is the 3,330 minimum, the Lightning
        seller's bond 1,500; the buyer never gives its payout info (WFI expiry)."""
        from api.escrow import bonds, flow
        from api.logics import Logics
        from api.models import Currency, LNPayment, OnchainBond

        buyer, seller = _user(), _user()
        now = timezone.now()
        currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        order = Order.objects.create(
            currency=currency, type=Order.Types.BUY, status=Order.Status.WFB, escrow_type=Order.EscrowTypes.ONCHAIN,
            created_at=now, expires_at=now + timezone.timedelta(hours=24), has_range=False, amount=100,
            satoshis=50_000, t0_satoshis=50_000, last_satoshis=50_000, maker=buyer, bond_size=3, is_explicit=True,
        )
        self._pay_bond(order, buyer, self._keys(order, "buyer"))
        bond = flow.bond_of(order, "maker")
        self.assertEqual(bond.value, bonds.min_bond_sats())  # 3,330 with a p2wpkh coordinator address
        penalty_fee = bond.value - bonds.penalty_sats(bond)
        self.assertLessEqual(penalty_fee, bond.value // 10)  # beta21: 1,500 of 3,330
        order = self._ln_take(order, seller)
        LNPayment.objects.filter(pk=order.taker_bond.pk).update(num_satoshis=1_500)
        order.refresh_from_db()
        order.trade_escrow = LNPayment.objects.create(
            concept=LNPayment.Concepts.TRESCROW, type=LNPayment.Types.HOLD, sender=seller,
            status=LNPayment.Status.LOCKED, num_satoshis=50_085, payment_hash=f"esc{order.id}{now.timestamp()}",
            preimage="00", invoice="lnbcrt1", created_at=now, expires_at=now + timezone.timedelta(hours=3),
        )
        order.status = Order.Status.WFI
        order.save()
        with patch("api.logics.LNNode") as ln:
            ln.cancel_return_hold_invoice.return_value = True
            ln.double_check_htlc_is_settled.return_value = True
            Logics.order_expires(order)
        order.refresh_from_db()
        bond.refresh_from_db()
        self.assertEqual(order.status, Order.Status.EXP)
        self.assertEqual(bond.status, OnchainBond.Status.PENALIZED)
        _mine()
        got = self._coordinator_got(bond.spend_txid)
        self.assertEqual(got, bonds.penalty_sats(bond))
        self.assertEqual(self._rewards(buyer), 0)
        self.assertEqual(self._rewards(seller), 750)
        self.assertEqual(order.proceeds, got - 750)
        self.assertEqual(order.taker_bond.status, LNPayment.Status.RETNED)

    def test_pure_onchain_taker_never_funds(self):
        """Pure onchain BUY of 1,000,000: the seller (taker) never funds; its 30,000 bond's
        penalty pays the coordinator; the buyer (maker) gets half of it, capped at its bond."""
        from api.escrow import flow
        from api.logics import Logics

        order, buyer, seller, keys = self._make_and_take()
        for r, u in (("seller", seller), ("buyer", buyer)):
            flow.action(order, u, "escrow_keys", {"escrow_pubkey": pubkey(keys[r]["escrow"]).hex(), "funding_pubkey": pubkey(keys[r]["funding"]).hex()})
            flow.action(order, u, "escrow_address", {"address": _faucet("getnewaddress", "", "bech32m")})
        flow.advance(order)
        order.refresh_from_db()
        order.expires_at = timezone.now()
        order.save()
        Logics.order_expires(order)
        order.refresh_from_db()
        tbond, mbond = flow.bond_of(order, "taker"), flow.bond_of(order, "maker")
        self.assertEqual(tbond.status, tbond.Status.PENALIZED)
        self.assertEqual(mbond.status, mbond.Status.RETURNABLE)
        _mine()
        got = self._coordinator_got(tbond.spend_txid)
        reward = int(min(got, mbond.value) * 0.5)
        self.assertEqual(self._rewards(buyer), reward)
        self.assertEqual(self._rewards(seller), 0)
        self.assertEqual(order.proceeds, got - reward)
        self.assertLessEqual(tbond.value - got, math.ceil(tbond.value * 0.1))
