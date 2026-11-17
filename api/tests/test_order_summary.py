"""Roblosats rl31-beta14: final summary of an order and the robot's past orders.

Mocked (no node): Lightning success for both sides, a maker that cancels a taken order, a
taker that left an order losing its bond, a Lightning dispute, onchain expiry (penalized /
recoverable bonds), an onchain cooperative release with the buyer fee out of its bond, an
onchain dispute and collaborative cancel, and the authenticated /api/pastorders/ endpoint.
"""

import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from api import order_summary
from api.escrow.taproot import Transaction, TxIn, TxOut
from api.models import Currency, LNPayment, OnchainBond, OnchainEscrow, Order

SELLER_SPK = "5120" + "a1" * 32
BUYER_SPK = "5120" + "b2" * 32
COORD_SPK = "5120" + "c3" * 32


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


def _ln(concept, sender, sats, status, receiver=None, **kw):
    now = timezone.now()
    return LNPayment.objects.create(
        concept=concept,
        type=LNPayment.Types.HOLD if concept != LNPayment.Concepts.PAYBUYER else LNPayment.Types.NORM,
        sender=sender,
        receiver=receiver,
        invoice="lnbc" + uuid.uuid4().hex,
        payment_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        num_satoshis=sats,
        status=status,
        created_at=now,
        expires_at=now + timezone.timedelta(hours=1),
        **kw,
    )


class SummaryBase(TestCase):
    def setUp(self):
        User.objects.get_or_create(username="admin")
        self.currency, _ = Currency.objects.get_or_create(currency=2, defaults={"exchange_rate": 100_000})
        self.maker, self.taker = _user(), _user()

    def _order(self, maker_sells=True, status=Order.Status.SUC, onchain=False, taker=True):
        now = timezone.now()
        return Order.objects.create(
            currency=self.currency,
            type=Order.Types.SELL if maker_sells else Order.Types.BUY,
            status=status,
            escrow_type=Order.EscrowTypes.ONCHAIN if onchain else Order.EscrowTypes.LIGHTNING,
            created_at=now,
            expires_at=now + timezone.timedelta(hours=24),
            has_range=False,
            amount=100,
            satoshis=100_000,
            t0_satoshis=100_000,
            last_satoshis=100_000,
            is_explicit=True,
            maker=self.maker,
            taker=self.taker if taker else None,
            bond_size=3,
        )

    def _bond(self, order, owner, role, status, value=3_000, **kw):
        return OnchainBond.objects.create(
            order=order, owner=owner, role=role, owner_pubkey="02" + "11" * 32,
            coordinator_pubkey="02" + "22" * 32, refund_csv=144, address=f"bcrt1p{role}",
            script_pubkey="5120" + "33" * 32, required_sats=value, txid=uuid.uuid4().hex * 2,
            vout=0, value=value, status=status, **kw,
        )


class LightningSummary(SummaryBase):
    def test_success_both_sides(self):
        o = self._order(maker_sells=True)
        o.maker_bond = _ln(LNPayment.Concepts.MAKEBOND, self.maker, 3_000, LNPayment.Status.RETNED)
        o.taker_bond = _ln(LNPayment.Concepts.TAKEBOND, self.taker, 3_000, LNPayment.Status.RETNED)
        o.trade_escrow = _ln(LNPayment.Concepts.TRESCROW, self.maker, 100_175, LNPayment.Status.SETLED)
        o.payout = _ln(LNPayment.Concepts.PAYBUYER, None, 99_000, LNPayment.Status.SUCCED,
                       receiver=self.taker, routing_budget_sats=500, preimage="ab" * 32)
        o.save()
        seller = order_summary.final_summary(o, self.maker)
        self.assertEqual(seller["outcome"], "success")
        self.assertEqual(seller["escrow_type"], "lightning")
        self.assertEqual(seller["sent_sats"], 100_175)
        self.assertEqual(seller["coordinator_fee_sats"], 175)
        self.assertEqual(seller["bond_state"], "returned")
        self.assertEqual(seller["bond_returned_sats"], 3_000)
        buyer = order_summary.final_summary(o, self.taker)
        self.assertTrue(buyer["is_buyer"])
        self.assertEqual(buyer["received_sats"], 99_000)
        self.assertEqual(buyer["routing_fee_sats"], 500)
        self.assertEqual(buyer["coordinator_fee_sats"], 500)
        self.assertIn("payout_preimage", [h["label"] for h in buyer["hashes"]])
        self.assertIsNone(order_summary.final_summary(o, _user()))

    def test_maker_cancels_taken_order(self):
        o = self._order(status=Order.Status.UCA)
        o.maker_bond = _ln(LNPayment.Concepts.MAKEBOND, self.maker, 3_000, LNPayment.Status.SETLED)
        o.taker_bond = _ln(LNPayment.Concepts.TAKEBOND, self.taker, 3_000, LNPayment.Status.RETNED)
        o.save()
        s = order_summary.final_summary(o, self.maker)
        self.assertEqual(s["outcome"], "cancelled")
        self.assertEqual(s["bond_lost_sats"], 3_000)
        self.assertEqual(s["bond_lost_reason"], "cancelled_after_taken")
        t = order_summary.final_summary(o, self.taker)
        self.assertEqual(t["bond_state"], "returned")
        self.assertEqual(t["bond_lost_sats"], 0)

    def test_taker_that_left_is_listed(self):
        o = self._order(status=Order.Status.PUB, taker=False)
        _ln(LNPayment.Concepts.TAKEBOND, self.taker, 3_000, LNPayment.Status.SETLED,
            description=f"Kilobot - Taking 'Order {o.id}' to BUY BTC for 100.0EUR")
        past = order_summary.past_orders(self.taker)
        self.assertEqual([p["order_id"] for p in past], [o.id])
        self.assertEqual(past[0]["outcome"], "taker_left")
        self.assertEqual(past[0]["bond_lost_sats"], 3_000)
        self.assertEqual(past[0]["bond_lost_reason"], "taker_left")
        # the maker's order is still public: not in its history
        self.assertEqual(order_summary.past_orders(self.maker), [])

    def test_dispute_lightning(self):
        o = self._order(status=Order.Status.MLD)
        o.maker_bond = _ln(LNPayment.Concepts.MAKEBOND, self.maker, 3_000, LNPayment.Status.SETLED)
        o.taker_bond = _ln(LNPayment.Concepts.TAKEBOND, self.taker, 3_000, LNPayment.Status.SETLED)
        o.trade_escrow = _ln(LNPayment.Concepts.TRESCROW, self.maker, 100_175, LNPayment.Status.SETLED)
        o.save()
        loser = order_summary.final_summary(o, self.maker)
        self.assertEqual(loser["outcome"], "dispute_lost")
        self.assertEqual(loser["bond_lost_reason"], "dispute_lost")
        self.assertEqual(loser["sent_sats"], 100_175)
        winner = order_summary.final_summary(o, self.taker)
        self.assertEqual(winner["outcome"], "dispute_won")
        self.assertEqual(winner["bond_state"], "rewards")
        self.assertEqual(winner["bond_lost_sats"], 0)


class OnchainSummary(SummaryBase):
    def _escrow(self, o, release_outputs, kind, status=OnchainEscrow.Status.RELEASED, cancel=False, plan=None):
        tx = Transaction([TxIn("dd" * 32, 0)], [TxOut(v, bytes.fromhex(spk)) for v, spk in release_outputs])
        sigs = {
            "plan": plan,
            "seller_payout_spk": SELLER_SPK,
            "buyer_payout_spk": BUYER_SPK,
        }
        if cancel:
            sigs["cancel"] = True
        return OnchainEscrow.objects.create(
            order=o, status=status, escrow_sats=sum(v for v, _ in release_outputs) + 300,
            funding_txid="ee" * 32, funding_signatures=sigs, release_kind=kind,
            release_tx=tx.serialize().hex(), release_txid="ff" * 32,
        )

    def _plan(self, sb, bb, fee_from_bond=False):
        return {
            "bond_ids": {"seller": sb.pk, "buyer": bb.pk},
            "bond_in_escrow": {"seller": 3_000, "buyer": 1_700 if fee_from_bond else 3_000},
            "trade_sats": 100_000, "seller_fee": 1_000, "buyer_fee": 1_000,
            "seller_mining": 250, "buyer_mining": 300, "buyer_fee_from_bond": fee_from_bond,
        }

    def test_expired_penalized_and_recoverable(self):
        o = self._order(status=Order.Status.EXP, onchain=True)
        o.expiry_reason = Order.ExpiryReasons.NESCRO
        o.save()
        self._bond(o, self.maker, "maker", OnchainBond.Status.PENALIZED, spend_txid="12" * 32)
        self._bond(o, self.taker, "taker", OnchainBond.Status.RETURNABLE)
        s = order_summary.final_summary(o, self.maker)
        self.assertEqual(s["escrow_type"], "onchain")
        self.assertEqual(s["bond_state"], "lost")
        self.assertEqual(s["bond_lost_reason"], "escrow_not_locked")
        self.assertIn("bond_penalty", [t["label"] for t in s["txs"]])
        t = order_summary.final_summary(o, self.taker)
        self.assertEqual(t["bond_state"], "recoverable")
        self.assertEqual(t["bond_lost_sats"], 0)

    def test_cooperative_release_fee_from_bond(self):
        o = self._order(maker_sells=True, onchain=True)
        sb = self._bond(o, self.maker, "maker", OnchainBond.Status.IN_DEPOSIT)
        bb = self._bond(o, self.taker, "taker", OnchainBond.Status.IN_DEPOSIT)
        # buyer: trade + what its bond left (minus the release mining), seller: its bond
        self._escrow(o, [(100_000 + 1_700 - 300, BUYER_SPK), (3_000, SELLER_SPK)],
                     OnchainEscrow.ReleaseKind.COOPERATIVE, plan=self._plan(sb, bb, fee_from_bond=True))
        seller = order_summary.final_summary(o, self.maker)
        self.assertEqual(seller["sent_sats"], 100_000 + 1_000 + 250)
        self.assertEqual(seller["bond_state"], "returned")
        self.assertEqual(seller["bond_returned_sats"], 3_000)
        self.assertEqual(seller["coordinator_fee_sats"], 1_000)
        self.assertEqual(seller["mining_fee_sats"], 250)
        self.assertEqual(seller["returned_sats"], 0)
        buyer = order_summary.final_summary(o, self.taker)
        self.assertTrue(buyer["fee_from_bond"])
        self.assertEqual(buyer["received_sats"], 100_000 - 300)
        self.assertEqual(buyer["bond_returned_sats"], 1_700)
        self.assertEqual(buyer["bond_lost_sats"], 0)
        self.assertEqual(buyer["mining_fee_sats"], 300 + 300)
        self.assertEqual([t["label"] for t in buyer["txs"]][-2:], ["deposit", "release"])

    def test_dispute_buyer_wins(self):
        o = self._order(maker_sells=True, onchain=True, status=Order.Status.MLD)
        sb = self._bond(o, self.maker, "maker", OnchainBond.Status.IN_DEPOSIT)
        bb = self._bond(o, self.taker, "taker", OnchainBond.Status.IN_DEPOSIT)
        self._escrow(o, [(100_000 + 3_000 + 1_500 - 300, BUYER_SPK), (1_500, COORD_SPK)],
                     OnchainEscrow.ReleaseKind.BUYER_WINS, plan=self._plan(sb, bb))
        seller = order_summary.final_summary(o, self.maker)
        self.assertEqual(seller["outcome"], "dispute_lost")
        self.assertEqual(seller["bond_state"], "lost")
        self.assertEqual(seller["bond_lost_sats"], 3_000)
        self.assertEqual(seller["bond_lost_reason"], "dispute_lost")
        buyer = order_summary.final_summary(o, self.taker)
        self.assertEqual(buyer["outcome"], "dispute_won")
        self.assertEqual(buyer["received_sats"], 100_000 - 300)
        self.assertEqual(buyer["bond_state"], "returned")
        self.assertEqual(buyer["rewards_sats"], 1_500)

    def test_collaborative_cancel(self):
        o = self._order(maker_sells=False, onchain=True, status=Order.Status.CCA)
        bb = self._bond(o, self.maker, "maker", OnchainBond.Status.IN_DEPOSIT)
        sb = self._bond(o, self.taker, "taker", OnchainBond.Status.IN_DEPOSIT)
        self._escrow(o, [(100_000 + 3_000 - 300, SELLER_SPK), (3_000, BUYER_SPK)],
                     OnchainEscrow.ReleaseKind.COOPERATIVE, status=OnchainEscrow.Status.REFUNDED,
                     cancel=True, plan=self._plan(sb, bb))
        seller = order_summary.final_summary(o, self.taker)
        self.assertEqual(seller["outcome"], "collab_cancelled")
        self.assertEqual(seller["returned_sats"], 100_000 - 300)
        self.assertEqual(seller["bond_returned_sats"], 3_000)
        buyer = order_summary.final_summary(o, self.maker)
        self.assertEqual(buyer["received_sats"], 0)
        self.assertEqual(buyer["bond_returned_sats"], 3_000)


class PastOrdersEndpoint(SummaryBase):
    def test_endpoint(self):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from api.views import PastOrdersView

        done = self._order(status=Order.Status.EXP, taker=False)
        done.maker_bond = _ln(LNPayment.Concepts.MAKEBOND, self.maker, 3_000, LNPayment.Status.CANCEL)
        done.save()
        self._order(status=Order.Status.CHA)  # active: not listed
        req = APIRequestFactory().get("/api/pastorders/")
        force_authenticate(req, user=self.maker)
        res = PastOrdersView.as_view()(req)
        self.assertEqual(res.status_code, 200)
        self.assertEqual([o["order_id"] for o in res.data["orders"]], [done.id])
        self.assertEqual(res.data["orders"][0]["outcome"], "expired")
        self.assertEqual(res.data["orders"][0]["bond_state"], "returned")
        anon = APIRequestFactory().get("/api/pastorders/")
        self.assertIn(PastOrdersView.as_view()(anon).status_code, (401, 403))

    def test_order_get_has_final_summary(self):
        with patch("api.views.Logics.summarize_trade", return_value=(False, {})):
            from rest_framework.test import APIRequestFactory, force_authenticate

            from api.views import OrderView

            o = self._order(status=Order.Status.EXP, taker=False)
            o.maker_bond = _ln(LNPayment.Concepts.MAKEBOND, self.maker, 3_000, LNPayment.Status.CANCEL)
            o.expiry_reason = Order.ExpiryReasons.NTAKEN
            o.save()
            req = APIRequestFactory().get(f"/api/order/?order_id={o.id}")
            force_authenticate(req, user=self.maker)
            res = OrderView.as_view({"get": "get"})(req)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.data["final_summary"]["outcome"], "expired")


class ReleaseNonceRound(SummaryBase):
    """A trader that lost its secret nonce sends a new one: a new signing round for both."""

    def _releasing(self):
        o = self._order(status=Order.Status.PAY, onchain=True)
        return OnchainEscrow.objects.create(
            order=o, status=OnchainEscrow.Status.RELEASING,
            release_kind=OnchainEscrow.ReleaseKind.COOPERATIVE,
            release_nonces={"buyer": "02" * 66, "seller": "03" * 66},
            release_partial_sigs={"seller": "aa" * 32},
        )

    def test_new_nonce_starts_a_new_round(self):
        from api.escrow import service

        e = service.submit_release_nonce(self._releasing(), "buyer", "04" * 66)
        e.refresh_from_db()
        self.assertEqual(e.release_nonces, {"buyer": "04" * 66})
        self.assertEqual(e.release_partial_sigs, {})
        self.assertEqual(e.funding_signatures["release_round"], 1)

    def test_signer_cannot_change_its_nonce(self):
        from api.escrow import service
        from api.escrow.service import EscrowError

        with self.assertRaises(EscrowError):
            service.submit_release_nonce(self._releasing(), "seller", "05" * 66)

    def test_same_nonce_again_is_harmless(self):
        from api.escrow import service

        e = service.submit_release_nonce(self._releasing(), "buyer", "02" * 66)
        e.refresh_from_db()
        self.assertEqual(e.release_partial_sigs, {"seller": "aa" * 32})
