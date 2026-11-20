"""Roblosats 0.9.9 (mocked, no node): legacy receive addresses and per-type dust, the deposit
change at the payout address, the fair deadline, the mempool notice of a pretaker, the alert
text of a sat order and the validators written to the migrations by setting name. The flows
against a node are in test_escrow_service_regtest.py (EscrowLegacyPayoutRegtest,
EscrowFairDeadlineRegtest, EscrowLegacyBondReturnRegtest)."""

import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from api.escrow import flow, transactions as T
from api.escrow.service import EscrowError
from api.escrow.taproot import EscrowContract, TxOut, pubkey
from api.models import Currency, OnchainBond, OnchainEscrow, Order

P2PKH = bytes.fromhex("76a914" + "11" * 20 + "88ac")
P2SH = bytes.fromhex("a914" + "22" * 20 + "87")
P2WPKH = bytes.fromhex("0014" + "33" * 20)
P2WSH = bytes.fromhex("0020" + "44" * 32)
P2TR = bytes.fromhex("5120" + "55" * 32)


def _user():
    u = User.objects.create_user(username=f"robot_{uuid.uuid4().hex[:8]}", password="x")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    return u


class Dust(SimpleTestCase):
    def test_core_thresholds_at_3000(self):
        self.assertEqual(T.dust_threshold(P2PKH, 3000), 546)
        self.assertEqual(T.dust_threshold(P2SH, 3000), 540)
        self.assertEqual(T.dust_threshold(P2WPKH, 3000), 294)
        self.assertEqual(T.dust_threshold(P2WSH, 3000), 330)
        self.assertEqual(T.dust_threshold(P2TR, 3000), 330)

    def test_follows_the_node_and_never_below_the_old_floor(self):
        with patch.object(T, "dust_relay_hook", lambda: 3000):
            self.assertEqual(T.dust_for(P2WPKH), 330)  # as before 0.9.9
            self.assertEqual(T.dust_for(P2TR), 330)
            self.assertEqual(T.dust_for(P2PKH), 546)
        with patch.object(T, "dust_relay_hook", lambda: 6000):
            self.assertEqual(T.dust_for(P2PKH), 1092)
            self.assertEqual(T.dust_for(P2TR), 660)

    def test_node_value_is_read_from_getmempoolinfo(self):
        from api.escrow import service

        service._dust_relay.update(at=0.0, value=None)
        with patch("api.escrow.service.rpc", return_value={"dustrelayfee": 0.00004}):
            self.assertEqual(service.dust_relay_fee(), 4000)
        service._dust_relay.update(at=0.0, value=None)
        with patch("api.escrow.service.rpc", side_effect=EscrowError("down")):
            self.assertEqual(service.dust_relay_fee(), 3000)
        service._dust_relay.update(at=0.0, value=None)

    def test_output_sizes_per_type(self):
        self.assertEqual([T.output_vbytes(s) for s in (P2PKH, P2SH, P2WPKH, P2TR)], [34, 32, 31, 43])

    def test_release_drops_a_legacy_output_below_its_dust(self):
        with patch.object(T, "dust_relay_hook", lambda: 3000):
            c = _contract()
            tx = T.release_tx(c, "ab" * 32, 0, 100_500, [TxOut(100_000, P2TR), TxOut(500, P2PKH)], 58, 1.0)
            self.assertEqual([o.script_pubkey for o in tx.outputs], [P2TR])  # 500 < 546
            tx = T.release_tx(c, "ab" * 32, 0, 100_600, [TxOut(100_000, P2TR), TxOut(600, P2PKH)], 58, 1.0)
            self.assertEqual(len(tx.outputs), 2)

    def test_bond_return_to_a_legacy_address_respects_its_dust(self):
        with patch.object(T, "dust_relay_hook", lambda: 3000):
            vb = T.coins_spend_vbytes(1, P2PKH)
            with self.assertRaises(ValueError):
                T.coins_spend_tx([("ab" * 32, 0, 545 + vb)], P2PKH, 1.0)
            tx = T.coins_spend_tx([("ab" * 32, 0, 546 + vb)], P2PKH, 1.0)
            self.assertEqual(tx.outputs[0].value, 546)


def _contract():
    k = [pubkey(bytes([i]) * 32) for i in (1, 2, 3)]
    return EscrowContract(k[0], k[1], k[2], 144, hrp="bcrt", rescue_plan=2)


class ReceiveAddresses(SimpleTestCase):
    def _spk(self, info):
        with patch("api.escrow.flow.service.rpc", return_value=info):
            return flow._spk_for_address("x")

    def test_legacy_and_segwit_accepted(self):
        for spk in (P2PKH, P2SH, P2WPKH, P2WSH, P2TR):
            self.assertEqual(self._spk({"isvalid": True, "scriptPubKey": spk.hex()}), spk)

    def test_other_scripts_refused(self):
        for bad in ("5220" + "66" * 32, "6a0401020304", "51"):  # witness v2, OP_RETURN, bare
            with self.assertRaises(EscrowError) as e:
                self._spk({"isvalid": True, "scriptPubKey": bad})
            self.assertIn("1…", str(e.exception))
        with self.assertRaises(EscrowError):
            self._spk({"isvalid": False})


def _utxo(v, spk):
    return T.Utxo("cd" * 32, 0, v, spk)


class DepositChange(SimpleTestCase):
    """0.9.9: a role in payout_change_roles gets its change at its payout script; below that
    script's dust it goes to the miners (never into the escrow)."""

    def _plan(self, seller_in, payout, roles, extra=True):
        fund = T.funding_script_pubkey(pubkey(b"\x07" * 32))
        with patch.object(T, "dust_relay_hook", lambda: 3000):
            return T.plan_deposit(
                [_utxo(seller_in, fund)], [], _contract(), 1_000_000, 200, 0, P2TR, 0, None,
                payout if roles else fund, fund, 2.0, fees_in_escrow=True, extra_to_escrow=extra,
                seller_bond=T.BondInput(T.Utxo("ef" * 32, 1, 30_000, P2TR), None),
                payout_change_roles=roles,
            )

    def _need(self):
        p = self._plan(2_000_000, P2PKH, {"seller"})
        return p.seller_pays

    def test_change_to_the_payout_address(self):
        p = self._plan(1_010_000, P2PKH, {"seller"})
        outs = [(o.value, o.script_pubkey) for o in p.tx.outputs[1:]]
        self.assertEqual(outs, [(1_010_000 - p.seller_pays, P2PKH)])
        self.assertEqual(p.extra_in_escrow, {})

    def test_small_change_goes_to_the_miners_not_the_escrow(self):
        need = self._need()
        # 400 sats over: below the P2PKH dust (546), with the change output's own cost too
        p = self._plan(need + 400, P2PKH, {"seller"})
        self.assertEqual(len(p.tx.outputs), 1)
        self.assertEqual(p.extra_in_escrow, {})
        self.assertEqual(p.seller_pays, need + 400)

    def test_older_apps_keep_the_extra_in_the_escrow(self):
        p = self._plan(self._need() + 400, P2PKH, set())
        self.assertEqual(len(p.tx.outputs), 1)
        self.assertTrue(p.extra_in_escrow.get("seller"))


class Base(TestCase):
    def setUp(self):
        self.patches = [
            patch("api.logics.send_notification"),
            patch("api.logics.nostr_send_order_event"),
            patch("api.models.order.send_notification"),
        ]
        for p in self.patches:
            p.start()
        self.currency, _ = Currency.objects.get_or_create(currency=1000, defaults={"exchange_rate": Decimal("0.0021")})
        now = timezone.now()
        self.maker, self.taker = _user(), _user()
        self.order = Order.objects.create(
            currency=self.currency, type=Order.Types.SELL, status=Order.Status.WFE,
            escrow_type=Order.EscrowTypes.ONCHAIN, created_at=now, expires_at=now + timezone.timedelta(minutes=10),
            has_range=False, amount=1_000_000, satoshis=1_000_000, t0_satoshis=1_000_000, last_satoshis=1_000_000,
            is_explicit=True, maker=self.maker, taker=self.taker, bond_size=3, sats_denominated=True,
            payment_method="Spamcoin On-Chain", spamcoin_sats=2100,
        )
        self.escrow = OnchainEscrow.objects.create(
            order=self.order, coordinator_pubkey="02" + "11" * 32, rescue_csv=144,
            funding_signatures={"fee_plan": 2, "rescue_plan": 2, "extra_plan": 1},
        )

    def tearDown(self):
        for p in self.patches:
            p.stop()


class FairDeadline(Base):
    def test_extends_to_an_hour_for_the_peer(self):
        self.assertTrue(flow.give_time(self.order, self.escrow, ["buyer"], "test"))
        self.order.refresh_from_db()
        self.assertGreater(self.order.expires_at, timezone.now() + timezone.timedelta(minutes=59))
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.funding_signatures["deadline_extensions"], 1)

    def test_not_when_there_is_an_hour_left(self):
        self.order.expires_at = timezone.now() + timezone.timedelta(hours=2)
        self.order.save(update_fields=["expires_at"])
        self.assertFalse(flow.give_time(self.order, self.escrow, ["buyer"], "test"))

    def test_not_once_expired_nor_outside_the_setup(self):
        self.order.expires_at = timezone.now() - timezone.timedelta(seconds=1)
        self.order.save(update_fields=["expires_at"])
        self.assertFalse(flow.give_time(self.order, self.escrow, ["buyer"], "test"))
        self.order.expires_at = timezone.now() + timezone.timedelta(minutes=5)
        self.order.status = Order.Status.CHA
        self.order.save(update_fields=["expires_at", "status"])
        self.assertFalse(flow.give_time(self.order, self.escrow, ["buyer"], "test"))

    def test_capped(self):
        with patch("api.escrow.flow.config", side_effect=lambda k, cast=None, default=None: default):
            for i in range(6):
                self.order.expires_at = timezone.now() + timezone.timedelta(minutes=5)
                self.order.save(update_fields=["expires_at"])
                self.assertTrue(flow.give_time(self.order, self.escrow, ["seller"], f"step {i}"))
            self.order.expires_at = timezone.now() + timezone.timedelta(minutes=5)
            self.order.save(update_fields=["expires_at"])
            self.assertFalse(flow.give_time(self.order, self.escrow, ["seller"], "one too many"))

    def test_only_when_the_signer_is_done_and_the_peer_owes(self):
        def waiting(order, escrow, role):
            return {"seller": ("peer", "rescue_signature"), "buyer": ("you", "rescue_signature")}[role]

        with patch("api.escrow.flow.waiting_for", side_effect=waiting):
            self.assertEqual(flow.peer_still_owes(self.order, self.escrow, "seller"), ["buyer"])
            self.assertEqual(flow.peer_still_owes(self.order, self.escrow, "buyer"), [])
        with patch("api.escrow.flow.waiting_for", return_value=("you", "funding")):
            self.assertEqual(flow.peer_still_owes(self.order, self.escrow, "seller"), [])


class PretakerMempoolNotice(Base):
    def test_the_onchain_pretaker_is_told(self):
        from api.notifications import Notifications

        pre = _user()
        OnchainBond.objects.create(
            order=self.order, owner=pre, role="taker", owner_pubkey="02" + "11" * 32,
            coordinator_pubkey="02" + "22" * 32, refund_csv=144, address="bcrt1pbond",
            script_pubkey="5120" + "33" * 32, required_sats=30_000,
        )
        with patch.object(Notifications, "send_message") as send:
            Notifications().onchain_seen_mempool(self.order, pre.id, "bond", 30_000, short=0)
        self.assertEqual(send.call_args.args[1], pre.robot)
        self.assertIn("bond", send.call_args.args[2])

    def test_a_stranger_is_not(self):
        from api.notifications import Notifications

        with patch.object(Notifications, "send_message") as send:
            Notifications().onchain_seen_mempool(self.order, _user().id, "bond", 30_000)
        send.assert_not_called()


class SatOrderAlert(Base):
    def test_sats_for_spamsats(self):
        from roblosats_admin import alerts

        with patch("roblosats_admin.messaging.coordinator_alias", return_value="Kilobot"):
            text = alerts.build_text(alerts.current(), "onchain", self.order, {"kind": "deposit_signed", "sats": 1_059_974})
        self.assertIn("SELL 1,000,000 sats for 2,100 Spamsats · Spamcoin On-Chain", text)
        self.assertNotIn("1000000 Spamcoin", text)


class SettingsValidators(TestCase):
    def test_written_by_name_and_validating_with_the_value(self):
        from django.conf import settings

        from api.models.validators import MaxFromSetting, MinFromSetting

        v = MaxFromSetting("MAX_TRADE", factor=2)
        self.assertEqual(v.limit_value, settings.MAX_TRADE * 2)
        self.assertEqual(v.deconstruct(), ("api.models.validators.MaxFromSetting", ("MAX_TRADE",), {"factor": 2}))
        self.assertEqual(MinFromSetting("MIN_TRADE").deconstruct()[1:], (("MIN_TRADE",), {}))

    def test_no_pending_migration_whatever_the_limits(self):
        from io import StringIO

        from django.core.management import call_command

        for lo, hi in ((20_000, 500_000_000), (101_000, 6_000_000)):
            with self.settings(MIN_TRADE=lo, MAX_TRADE=hi):
                out = StringIO()
                call_command("makemigrations", "api", "--check", "--dry-run", stdout=out)
                self.assertIn("No changes", out.getvalue())
