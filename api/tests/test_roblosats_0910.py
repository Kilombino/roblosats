"""0.9.10 server: onchain dispute notices, order links with the clearnet web client, the swap
payout fee floor, audit of ledger-only credits, clearer invalid-address answers."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from api import step_alerts as sa
from api.models import Order
from api.tests.test_step_alerts import Base, order

S = Order.Status


class DisputeNotices(Base):
    def test_penalize_seller(self):
        o = order(status=S.MLD, onchain=True)  # SELL: the maker sells
        with patch.object(sa, "_dispute_outcome", return_value="penalize_seller"):
            told = self.told(o, S.WFR, S.MLD)
        self.assertEqual(told, [(1, "dispute_penalized_seller", True), (2, "dispute_other_penalized_buyer", False)])

    def test_penalize_buyer(self):
        o = order(status=S.TLD, onchain=True)
        with patch.object(sa, "_dispute_outcome", return_value="penalize_buyer"):
            told = self.told(o, S.WFR, S.TLD)
        self.assertEqual(told, [(2, "dispute_penalized_buyer", False), (1, "dispute_other_penalized_seller", True)])

    def test_completed(self):
        o = order(status=S.PAY, onchain=True)
        self.assertEqual(self.told(o, S.WFR, S.PAY),
                         [(2, "dispute_completed_buyer", True), (1, "dispute_completed_seller", False)])

    def test_winner_unchanged(self):
        o = order(status=S.MLD, onchain=True)
        with patch.object(sa, "_dispute_outcome", return_value=""):
            self.assertEqual(self.told(o, S.WFR, S.MLD), [(1, "dispute_lost", False), (2, "dispute_won_buyer", False)])


class ClearnetLinks(SimpleTestCase):
    def test_order_url_has_both_links(self):
        from api.notifications import Notifications

        o = SimpleNamespace(id=29)
        with patch("api.notifications.config", side_effect=lambda k, default="", **kw: {
                "CLEARNET_ORDER_URL": "https://roblosats.kilombino.com/order/kilobot/"}.get(k, default)), \
                patch("api.notifications.get_federation_short_alias", return_value="kilobot"):
            n = Notifications.__new__(Notifications)
            n.site = "kiloblo.onion"
            url = n.order_url(o)
        self.assertEqual(url, "http://kiloblo.onion/order/kilobot/29 · https://roblosats.kilombino.com/order/kilobot/29")

    def test_no_clearnet(self):
        from api.notifications import clearnet_order_url

        o = SimpleNamespace(id=29)
        for value in ("", "http://plain.example/order/x", "  "):
            with patch("api.notifications.config", return_value=value):
                self.assertEqual(clearnet_order_url(o), "")

    def test_nostr_dm_carries_the_links(self):
        from api.notifications import Notifications

        n = Notifications.__new__(Notifications)
        robot = SimpleNamespace(id=1, nostr_pubkey="ab", telegram_enabled=False, telegram_chat_id=None,
                                webhook_enabled=False)
        with patch.object(Notifications, "save_message"), \
                patch("api.notifications.nostr_send_notification_event") as nostr:
            n.send_message(SimpleNamespace(id=29), robot, "⚖️ Order 29", "http://x.onion/order/k/29 · https://c/29")
        self.assertIn("https://c/29", nostr.delay.call_args.kwargs["text"])


class PayoutFeeFloor(SimpleTestCase):
    def test_default_and_configured(self):
        from api.logics import payout_min_fee_rate

        with patch("api.logics.config", side_effect=lambda k, cast=None, default=None: default):
            self.assertEqual(payout_min_fee_rate(), 1.0)
        with patch("api.logics.config", side_effect=lambda k, cast=None, default=None: 3.0):
            self.assertEqual(payout_min_fee_rate(), 3.0)
        with patch("api.logics.config", side_effect=lambda k, cast=None, default=None: 0.2):
            self.assertEqual(payout_min_fee_rate(), 1.0)  # LND sends whole sat/vB

    def test_hybrid_quote_uses_the_floor(self):
        from api import hybrid_swap

        with patch("api.utils.get_minning_fee", return_value=1), \
                patch("api.logics.Logics.compute_swap_fee_rate", return_value=1.0), \
                patch("control.models.BalanceLog.objects") as bl:
            bl.order_by.return_value.first.return_value = object()
            _swap, mining = hybrid_swap.current_rates()
        self.assertEqual(mining, 1.0)


class InvalidAddressAnswer(SimpleTestCase):
    def test_says_why(self):
        from api.utils import validate_onchain_address

        with patch("api.utils.bitcoind_rpc", return_value={"isvalid": False, "error": "Invalid checksum or length of Base58 address (P2PKH or P2SH)"}):
            ok, ctx = validate_onchain_address("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN")
        self.assertFalse(ok)
        self.assertIn("Invalid checksum", ctx["bad_address"])
        self.assertTrue(ctx["bad_address"].startswith("Invalid address"))


class AuditLedgerCredits(TestCase):
    def test_swap_surcharge_back_counts_as_credited(self):
        from django.contrib.auth.models import User

        from api.logics import ledger_credit
        from api.management.commands.audit_payouts import LEDGER_KINDS_NOT_IN_LOGS, ledger_credits

        u = User.objects.create_user(username="Seller370", password="x")
        ledger_credit(u.robot, 616, "swap_surcharge_back", "swap_back:25:x", order_id=None, note="unused part")
        self.assertEqual(ledger_credits(LEDGER_KINDS_NOT_IN_LOGS).get(u.robot.id), 616)
