"""0.9.9-1: the dispute page for on-chain escrows (pure, hybrid A, hybrid B) and Lightning."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from api.models import LNPayment, OnchainBond, OnchainEscrow, Order
from roblosats_admin import dispute_onchain, disputes
from roblosats_admin.tests.test_disputes import make_disputed_order, make_payment, make_user

BUYER_ADDR = "bc1qgs4r6uppskcgwx50kezggrnje4lyelpcutspwc"
SELLER_ADDR = "bc1q84dyzzx0h5xrnglc8f3ryx8cnaf3khqvpkt62x"
SPK = "0014" + "11" * 20
SPK2 = "0014" + "22" * 20

# no node in these tests: the dust threshold falls back to the default relay fee
DUST = patch("api.escrow.transactions.dust_relay_hook", lambda: 3000)


def bond(order, owner, role, value=30_000):
    return OnchainBond.objects.create(
        order=order, owner=owner, role=role, status=OnchainBond.Status.IN_DEPOSIT,
        owner_pubkey="02" + "11" * 32, coordinator_pubkey="02" + "22" * 32, refund_csv=540,
        address="bcrt1pbond" + role, script_pubkey="5120" + "33" * 32, required_sats=value, value=value,
        txid="ab" * 32,
    )


def onchain_order(maker, taker, hybrid_a=False, status=Order.Status.DIS, escrow_status=OnchainEscrow.Status.LOCKED,
                  addresses=("buyer", "seller"), extra=None, fee_plan=2):
    """A SELL order: the maker sells (escrow + bond on chain), the taker buys."""
    now = timezone.now()
    order = Order.objects.create(
        type=Order.Types.SELL, status=status, is_disputed=True, escrow_type=Order.EscrowTypes.ONCHAIN,
        created_at=now, expires_at=now + timezone.timedelta(hours=10), has_range=False, amount=100,
        satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000, payment_method="SEPA",
        maker=maker, taker=taker, bond_size=3, taker_lightning=hybrid_a,
    )
    seller_bond = bond(order, maker, "maker")
    bond_ids = {"seller": seller_bond.pk}
    if hybrid_a:
        order.taker_bond = make_payment(taker, LNPayment.Concepts.TAKEBOND, 30_000, status=LNPayment.Status.SETLED)
        order.save()
    else:
        bond_ids["buyer"] = bond(order, taker, "taker").pk
    data = {
        "fee_plan": fee_plan,
        "plan": {
            "bond_ids": bond_ids,
            "trade_sats": 1_000_000,
            "fees_in_escrow": {"seller": 200, "buyer": 1_701 if not hybrid_a else 0},
            "extra_in_escrow": extra or {"seller": 362, "buyer": 0},
            "bond_in_escrow": {} if hybrid_a else {"buyer": 27_896},
        },
    }
    for role in addresses:
        data[f"{role}_payout_address"] = BUYER_ADDR if role == "buyer" else SELLER_ADDR
        data[f"{role}_payout_spk"] = SPK if role == "buyer" else SPK2
    ex = extra or {"seller": 362, "buyer": 0}
    sats = 1_000_000 + 30_000 + (0 if hybrid_a else 27_896) + 200 + (0 if hybrid_a else 1_701) + ex["seller"] + ex["buyer"]
    OnchainEscrow.objects.create(order=order, status=escrow_status, escrow_sats=sats, funding_txid="cd" * 32,
                                 confirmed_height=100, rescue_csv=2016, funding_signatures=data)
    return order


class OnchainPlanTest(TestCase):
    def setUp(self):
        DUST.start()
        self.addCleanup(DUST.stop)
        self.maker = make_user("SellerBot")
        self.taker = make_user("BuyerBot")

    def text(self, p):
        return " ".join(p["lines"])

    def test_buyer_wins_on_chain(self):
        order = onchain_order(self.maker, self.taker)
        p = disputes.plan(order, "buyer")
        self.assertTrue(p["possible"], p["missing"])
        t = self.text(p)
        # winner: trade + own bond (what is left of it) + half the seller's bond
        self.assertIn(disputes.fmt_sats(1_000_000 + 27_896 + 15_000), t)
        self.assertIn(BUYER_ADDR, t)
        self.assertIn("buyer's app (BuyerBot) must sign", t)
        # the seller's extra (362) is above dust: back to the seller
        self.assertIn(f"{disputes.fmt_sats(362)} sats at {SELLER_ADDR}", t)
        # coordinator: the other half of the seller's bond + both fees
        self.assertIn(disputes.fmt_sats(15_000 + 200 + 1_701), t)
        self.assertIn(f"loses their on-chain bond ({disputes.fmt_sats(30_000)} sats)", t)

    def test_seller_wins_on_chain(self):
        order = onchain_order(self.maker, self.taker)
        p = disputes.plan(order, "seller")
        self.assertTrue(p["possible"])
        t = self.text(p)
        self.assertIn(disputes.fmt_sats(1_000_000 + 30_000 + 13_948 + 362), t)
        self.assertIn("seller's app (SellerBot) must sign the release", t)
        self.assertIn(SELLER_ADDR, t)

    def test_return_everything_on_chain(self):
        order = onchain_order(self.maker, self.taker)
        p = disputes.plan(order, "return")
        self.assertTrue(p["possible"])
        t = self.text(p)
        self.assertIn("seller's app (SellerBot) must sign the return", t)
        self.assertIn("The buyer does not sign anything", t)
        self.assertIn(disputes.fmt_sats(1_000_000 + 30_000 + 200 + 362), t)
        self.assertIn(f"{disputes.fmt_sats(27_896 + 1_701)} sats at {BUYER_ADDR}", t)
        self.assertIn("charges nothing", t)
        self.assertIn("collaboratively cancelled", t)

    def test_amounts_match_the_flow(self):
        """The page's numbers come from the same split the release uses."""
        from api.escrow import flow

        order = onchain_order(self.maker, self.taker)
        escrow = flow.escrow_of(order)
        outs, info = flow.dispute_split(order, escrow, True)
        self.assertEqual(sum(s for _w, s in outs), escrow.escrow_sats)
        self.assertEqual(sum(s for _w, s in flow.return_split(escrow)), escrow.escrow_sats)
        for who, sats in outs:
            self.assertIn(disputes.fmt_sats(sats), self.text(disputes.plan(order, "buyer")), who)

    def test_penalize_seller_on_chain(self):
        """0.9.10: no trade, the seller penalized: it gets its escrow amount back, the buyer its
        bond + half the seller's, the coordinator the other half; the seller's app signs."""
        from api.escrow import flow

        order = onchain_order(self.maker, self.taker)
        p = disputes.plan(order, "penalize_seller")
        self.assertTrue(p["possible"], p["missing"])
        t = self.text(p)
        self.assertIn("seller's app (SellerBot) must sign the release", t)
        self.assertIn(f"{disputes.fmt_sats(1_000_000 + 200 + 362)} sats at {SELLER_ADDR}", t)
        self.assertIn(f"{disputes.fmt_sats(27_896 + 15_000 + 1_701)} sats at {BUYER_ADDR}", t)
        self.assertIn(disputes.fmt_sats(15_000), t)
        self.assertIn("mining fee comes out of the coordinator's output", t)
        escrow = flow.escrow_of(order)
        outs, info = flow.penalize_split(order, escrow, "seller")
        self.assertEqual(sum(v for _w, v in outs), escrow.escrow_sats)
        self.assertEqual(dict(outs)["coordinator"], 15_000)
        self.assertEqual(info["fee_payer"], 2)

    def test_penalize_buyer_on_chain(self):
        from api.escrow import flow

        order = onchain_order(self.maker, self.taker)
        p = disputes.plan(order, "penalize_buyer")
        self.assertTrue(p["possible"], p["missing"])
        t = self.text(p)
        escrow = flow.escrow_of(order)
        outs, info = flow.penalize_split(order, escrow, "buyer")
        # the seller: escrow amount + its bond + half of the buyer's + its fee + its extra
        self.assertEqual(dict(outs)["seller"], 1_000_000 + 30_000 + 13_948 + 200 + 362)
        # the buyer keeps only its fee back (1 701 > dust)
        self.assertEqual(dict(outs)["buyer"], 1_701)
        self.assertEqual(dict(outs)["coordinator"], 27_896 - 13_948)
        self.assertEqual(sum(v for _w, v in outs), escrow.escrow_sats)
        self.assertIn(disputes.fmt_sats(dict(outs)["seller"]), t)
        self.assertIn("loses their on-chain bond", t)

    def test_completed_on_chain(self):
        from api.escrow import flow

        order = onchain_order(self.maker, self.taker)
        p = disputes.plan(order, "completed")
        self.assertTrue(p["possible"], p["missing"])
        t = self.text(p)
        self.assertIn("buyer's app (BuyerBot) must sign the release", t)
        escrow = flow.escrow_of(order)
        outs, info = flow.completed_split(order, escrow)
        self.assertEqual(dict(outs), {"buyer": 1_000_000 + 27_896, "seller": 30_000 + 362, "coordinator": 1_901})
        self.assertIn(f"{disputes.fmt_sats(1_027_896)} sats at {BUYER_ADDR}", t)
        self.assertIn(f"{disputes.fmt_sats(30_362)} sats at {SELLER_ADDR}", t)
        self.assertIn("successful trade", t)

    def test_unknown_decision_refused(self):
        order = onchain_order(self.maker, self.taker)
        self.assertFalse(disputes.plan(order, "nonsense")["possible"])

    def test_missing_address_and_unconfirmed_escrow(self):
        order = onchain_order(self.maker, self.taker, addresses=("seller",))
        p = disputes.plan(order, "buyer")
        self.assertFalse(p["possible"])
        self.assertIn("the buyer has not given an on-chain address to receive the sats", p["missing"])
        self.assertTrue(disputes.plan(order, "seller")["possible"])
        order = onchain_order(self.maker, self.taker, escrow_status=OnchainEscrow.Status.MEMPOOL)
        p = disputes.plan(order, "return")
        self.assertFalse(p["possible"])
        self.assertIn("not confirmed", p["missing"][0])

    def test_no_longer_in_dispute(self):
        order = onchain_order(self.maker, self.taker, status=Order.Status.CCA)
        self.assertIn("the order is no longer in dispute", disputes.plan(order, "return")["missing"])

    def test_releasing_is_replaced_with_a_warning(self):
        order = onchain_order(self.maker, self.taker, escrow_status=OnchainEscrow.Status.RELEASING)
        p = disputes.plan(order, "buyer")
        self.assertTrue(p["possible"])
        self.assertIn("replaces it", p["warning"])

    def test_buyer_dust_part_goes_with_the_seller(self):
        from api.escrow import flow

        order = onchain_order(self.maker, self.taker)
        escrow = flow.escrow_of(order)
        data = dict(escrow.funding_signatures)
        data["plan"] = {**data["plan"], "bond_in_escrow": {"buyer": 100}, "fees_in_escrow": {"seller": 200, "buyer": 0}}
        escrow.funding_signatures = data
        escrow.save()
        self.assertEqual([w for w, _s in flow.return_split(escrow)], ["seller"])
        self.assertIn("below the dust limit", self.text(disputes.plan(order, "return")))

    def test_hybrid_a(self):
        order = onchain_order(self.maker, self.taker, hybrid_a=True)
        p = disputes.plan(order, "buyer")
        self.assertTrue(p["possible"], p["missing"])
        self.assertIn(f"Lightning bond ({disputes.fmt_sats(30_000)} sats), settled when the dispute opened, is credited back", self.text(p))
        p = disputes.plan(order, "seller")
        self.assertIn(f"credits {disputes.fmt_sats(15_000)} sats of it to the seller", self.text(p))
        p = disputes.plan(order, "return")
        self.assertTrue(p["possible"])
        self.assertIn(f"taker's Lightning bond ({disputes.fmt_sats(30_000)} sats) goes back", self.text(p))
        p = disputes.plan(order, "penalize_buyer")  # 0.9.10: the Lightning buyer penalized
        self.assertTrue(p["possible"], p["missing"])
        self.assertIn(f"credits {disputes.fmt_sats(15_000)} sats of it to the seller", self.text(p))
        p = disputes.plan(order, "completed")
        self.assertTrue(p["possible"], p["missing"])

    def test_hybrid_a_without_the_lightning_bond(self):
        order = onchain_order(self.maker, self.taker, hybrid_a=True)
        order.taker_lightning = False  # neither an onchain nor a Lightning bond for the buyer
        order.save()
        self.assertIn("the buyer's bond is not in the escrow", disputes.plan(order, "return")["missing"])

    def test_hybrid_b_is_a_lightning_order_with_a_note(self):
        from api.escrow import flow

        order = make_disputed_order(Order.Types.BUY, self.maker, self.taker)  # the maker buys
        onchain = bond(order, self.maker, "maker", value=3_000)
        onchain.status = OnchainBond.Status.PENALIZED
        onchain.save()
        order.maker_bond = LNPayment.objects.create(
            payment_hash=f"{flow.PROXY_PREFIX}{onchain.pk}", concept=LNPayment.Concepts.MAKEBOND,
            type=LNPayment.Types.HOLD, status=LNPayment.Status.SETLED, sender=self.maker, num_satoshis=3_000,
            created_at=timezone.now(), expires_at=timezone.now() + timezone.timedelta(days=30),
        )
        order.save()
        with patch("api.escrow.bonds.penalty_sats", return_value=2_500):
            p = disputes.plan(order, "buyer")
        self.assertTrue(p["possible"])
        self.assertIn("penalized on chain when the dispute opened", p["lines"][-1])
        self.assertIn(disputes.fmt_sats(2_500), p["lines"][-1])
        self.assertTrue(disputes.plan(order, "penalize_buyer")["possible"])

    def test_lightning_plan_unchanged(self):
        order = make_disputed_order(Order.Types.SELL, self.maker, self.taker)
        for decision in disputes.DECISIONS:
            self.assertEqual(disputes.plan(order, decision), disputes._plan_lightning(order, decision), decision)

    def test_next_step(self):
        from api.escrow import flow

        order = onchain_order(self.maker, self.taker, status=Order.Status.CCA)
        self.assertIsNone(dispute_onchain.next_step(order))
        escrow = flow.escrow_of(order)
        escrow.status = OnchainEscrow.Status.RELEASING
        escrow.release_kind = OnchainEscrow.ReleaseKind.SELLER_WINS
        escrow.save()
        step = dispute_onchain.next_step(order)
        self.assertFalse(step["done"])
        self.assertIn("seller's signature (SellerBot)", step["text"])
        escrow.release_txid = "ef" * 32
        escrow.status = OnchainEscrow.Status.REFUNDED
        escrow.save()
        with patch("roblosats_admin.onchain.status_text", return_value="confirmed"):
            step = dispute_onchain.next_step(order)
        self.assertTrue(step["done"])
        self.assertIn("efefefef", str(step["link"]))


class WaitingForDisputeReleaseTest(TestCase):
    """#26: the side that does not sign a dispute release waits for the other, not for the
    coordinator ("signing the release together with your peer" with a spinner)."""

    def test_signer_and_the_other(self):
        from api.escrow import flow

        maker, taker = make_user("S"), make_user("B")
        with DUST:
            order = onchain_order(maker, taker, status=Order.Status.CCA,
                                  escrow_status=OnchainEscrow.Status.RELEASING)
        escrow = flow.escrow_of(order)
        escrow.release_kind = OnchainEscrow.ReleaseKind.SELLER_WINS
        self.assertEqual(flow.waiting_for(order, escrow, "seller"), ("you", "release_signature"))
        self.assertEqual(flow.waiting_for(order, escrow, "buyer"), ("peer", "release_signature"))
        escrow.release_kind = OnchainEscrow.ReleaseKind.BUYER_WINS
        self.assertEqual(flow.waiting_for(order, escrow, "buyer"), ("you", "release_signature"))
        self.assertEqual(flow.waiting_for(order, escrow, "seller"), ("peer", "release_signature"))


class HybridReturnEverythingTest(TestCase):
    """Until 0.9.9-1 "return everything" on a hybrid A order kept the taker's Lightning bond: it
    was settled when the dispute opened and return_bond on a settled invoice returns nothing."""

    def setUp(self):
        DUST.start()
        self.addCleanup(DUST.stop)
        self.maker, self.taker = make_user("S"), make_user("B")

    @patch("api.logics.ledger_credit")
    def test_settled_bond_is_credited(self, credit):
        from api.escrow import flow

        order = onchain_order(self.maker, self.taker, hybrid_a=True)
        flow.ln_bond_return_everything(order)
        credit.assert_called_once()
        robot, sats, kind, key = credit.call_args.args
        self.assertEqual((robot, sats, kind), (self.taker.robot, 30_000, "dispute_bond_back"))
        self.assertTrue(key.startswith(f"dispute_bond_back:{order.id}:"))

    @patch("api.logics.ledger_credit")
    @patch("api.logics.Logics.return_bond")
    def test_held_bond_is_unlocked(self, return_bond, credit):
        from api.escrow import flow

        order = onchain_order(self.maker, self.taker, hybrid_a=True)
        LNPayment.objects.filter(pk=order.taker_bond.pk).update(status=LNPayment.Status.LOCKED)

        def ret(b):
            LNPayment.objects.filter(pk=b.pk).update(status=LNPayment.Status.RETNED)

        return_bond.side_effect = ret
        flow.ln_bond_return_everything(order)
        return_bond.assert_called_once()
        credit.assert_not_called()


class OnchainDisputePageTest(TestCase):
    def setUp(self):
        DUST.start()
        self.addCleanup(DUST.stop)
        self.maker, self.taker = make_user("SellerBot"), make_user("BuyerBot")
        self.order = onchain_order(self.maker, self.taker)
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.page = reverse("admin:api_order_dispute", args=[self.order.pk])

    def test_buttons_available_and_translated(self):
        body = self.client.get(self.page, HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertNotIn("has not been settled by the coordinator", body)
        self.assertIn("buyer&#x27;s app (BuyerBot) must sign", body)
        body = self.client.get(self.page + "?confirm=return", HTTP_ACCEPT_LANGUAGE="es").content.decode()
        self.assertIn("la app del vendedor (SellerBot) tiene que firmar la devolución", body)
        self.client.cookies["django_language"] = "ca"
        body = self.client.get(self.page + "?confirm=buyer").content.decode()
        self.assertIn("l&#x27;app del comprador (BuyerBot) ha de signar", body)
        self.assertIn("Guanya el comprador", body)

    def test_every_language_renders(self):
        for lang in ("en", "es", "ca", "de", "fr", "it", "pt", "ru", "zh-hans", "ja"):
            self.client.cookies["django_language"] = lang
            for confirm in ("buyer", "return", "penalize_seller"):
                r = self.client.get(f"{self.page}?confirm={confirm}")
                self.assertEqual(r.status_code, 200, (lang, confirm))

    @patch("api.admin.send_notification")
    @patch("api.escrow.flow.return_everything")
    def test_resolve_return_runs_the_onchain_action(self, ret, _notify):
        r = self.client.post(self.page, {"action": "resolve", "decision": "return", "confirmed": "yes",
                                         "status": str(self.order.status)})
        self.assertEqual(r.status_code, 302)
        ret.assert_called_once()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CCA)
        msgs = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any("Done" in m and "onchain return started" in m for m in msgs), msgs)

    @patch("api.admin.send_notification")
    @patch("api.escrow.flow.resolve_dispute")
    def test_resolve_buyer_wins_maps_to_the_taker(self, resolve, _notify):
        self.client.post(self.page, {"action": "resolve", "decision": "buyer", "confirmed": "yes",
                                     "status": str(self.order.status)})
        resolve.assert_called_once()
        self.assertTrue(resolve.call_args.kwargs["buyer_wins"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.MLD)  # the maker (seller) lost

    @patch("api.escrow.flow.return_everything")
    def test_refused_decision_does_nothing(self, ret):
        escrow = self.order.onchain_escrow
        escrow.funding_signatures = {k: v for k, v in escrow.funding_signatures.items() if not k.startswith("seller_payout")}
        escrow.save()
        r = self.client.post(self.page, {"action": "resolve", "decision": "return", "confirmed": "yes",
                                         "status": str(self.order.status)})
        self.assertEqual(r.status_code, 302)
        ret.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.DIS)

    def test_takes_the_order_lock(self):
        from api import logics

        calls = []
        real = logics.acquire_order_lock

        def spy(order_id, space=7391):
            calls.append(order_id)
            return real(order_id, space)

        with patch("api.logics.acquire_order_lock", spy), patch("api.escrow.flow.return_everything"), \
                patch("api.admin.send_notification"):
            self.client.post(self.page, {"action": "resolve", "decision": "return", "confirmed": "yes",
                                         "status": str(self.order.status)})
        self.assertGreaterEqual(calls.count(self.order.pk), 2)  # the check and the action


class SpamcoinPriceTextTest(TestCase):
    def test_spamcoin_price_in_spamsats(self):
        from types import SimpleNamespace

        from roblosats_admin import onchain

        o = SimpleNamespace(currency_id=1000, currency="BTC", sats_denominated=True, fixed_price=0.0021,
                            contract_price=None)
        self.assertIn("210,000 Spamsats/BTC", onchain.price_text(o))
        o = SimpleNamespace(currency_id=2, currency="EUR", sats_denominated=False, fixed_price=90000.5,
                            contract_price=None)
        self.assertIn("90,000.50 EUR/BTC", onchain.price_text(o))


class OnchainPenalizeCompletedPageTest(TestCase):
    """0.9.10: the page's penalize and completed buttons run the new onchain admin actions."""

    def setUp(self):
        DUST.start()
        self.addCleanup(DUST.stop)
        self.maker, self.taker = make_user("SellerBot"), make_user("BuyerBot")
        self.order = onchain_order(self.maker, self.taker)
        self.admin = User.objects.create_superuser("panel-admin", "a@example.org", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.page = reverse("admin:api_order_dispute", args=[self.order.pk])

    def post(self, decision):
        return self.client.post(self.page, {"action": "resolve", "decision": decision, "confirmed": "yes",
                                            "status": str(self.order.status)})

    @patch("api.admin.send_notification")
    @patch("api.escrow.flow.penalize_dispute")
    def test_penalize_seller(self, pen, _n):
        self.post("penalize_seller")
        pen.assert_called_once()
        self.assertEqual(pen.call_args.args[1], "seller")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.MLD)  # the maker (seller) is guilty

    @patch("api.admin.send_notification")
    @patch("api.escrow.flow.penalize_dispute")
    def test_penalize_buyer(self, pen, _n):
        self.post("penalize_buyer")
        self.assertEqual(pen.call_args.args[1], "buyer")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.TLD)

    @patch("api.escrow.flow.complete_dispute")
    def test_completed(self, comp):
        r = self.post("completed")
        comp.assert_called_once()
        msgs = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any("successful trade onchain" in m for m in msgs), msgs)

    def test_confirmation_pages_render_in_es_ca(self):
        for lang, needle in (("es", "Penalizar al vendedor"), ("ca", "Penalitzar el venedor")):
            self.client.cookies["django_language"] = lang
            for d in ("penalize_seller", "penalize_buyer", "completed"):
                r = self.client.get(f"{self.page}?confirm={d}")
                self.assertEqual(r.status_code, 200)
            self.assertIn(needle, r.content.decode())


class DisputeSummaryTest(TestCase):
    """0.9.10: the final summary of the new dispute releases, per side."""

    def setUp(self):
        DUST.start()
        self.addCleanup(DUST.stop)
        self.maker, self.taker = make_user("SellerBot"), make_user("BuyerBot")

    def released(self, outcome, outs, kind, status, payer="seller", reward=0):
        from api.escrow.taproot import Transaction, TxIn, TxOut

        order = onchain_order(self.maker, self.taker, status=status)
        escrow = order.onchain_escrow
        spk = {"buyer": SPK, "seller": SPK2, "coordinator": "5120" + "99" * 32}
        tx = Transaction([TxIn("ab" * 32, 0)], [TxOut(v, bytes.fromhex(spk[w])) for w, v in outs])
        data = dict(escrow.funding_signatures)
        data.update(dispute_outcome=outcome, release_fee_payer=payer, dispute_reward_sats=reward,
                    cancel=outcome.startswith("penalize"), funding_txid="cd" * 32)
        data["plan"] = {**data["plan"], "seller_fee": 200, "buyer_fee": 1_701}
        escrow.funding_signatures = data
        escrow.release_tx = tx.serialize().hex()
        escrow.release_txid = tx.txid()
        escrow.release_kind = kind
        escrow.status = OnchainEscrow.Status.REFUNDED
        escrow.save()
        return order

    @patch("api.order_summary.tx_status", return_value="confirmed")
    def test_penalize_seller(self, _st):
        from api.order_summary import final_summary

        order = self.released("penalize_seller", [("seller", 1_000_562 - 400), ("buyer", 44_597), ("coordinator", 15_000 - 0)],
                              OnchainEscrow.ReleaseKind.SELLER_WINS, Order.Status.MLD, payer="seller", reward=15_000)
        b = final_summary(order, self.taker)
        self.assertEqual((b["bond_state"], b["bond_returned_sats"], b["rewards_sats"]), ("returned", 27_896, 15_000))
        self.assertEqual(b["bond_returned_to"], BUYER_ADDR)
        s = final_summary(order, self.maker)
        self.assertEqual((s["bond_state"], s["bond_lost_sats"]), ("lost", 30_000))
        self.assertEqual(s["returned_sats"], 1_000_000 - 400)

    @patch("api.order_summary.tx_status", return_value="confirmed")
    def test_completed(self, _st):
        from api.order_summary import final_summary

        order = self.released("completed", [("buyer", 1_027_896 - 300), ("seller", 30_362), ("coordinator", 1_901)],
                              OnchainEscrow.ReleaseKind.BUYER_WINS, Order.Status.SUC)
        b = final_summary(order, self.taker)
        self.assertEqual(b["received_sats"], 1_000_000 - 300)
        self.assertEqual(b["bond_state"], "returned")
        s = final_summary(order, self.maker)
        self.assertEqual((s["bond_state"], s["bond_lost_sats"]), ("returned", 0))
