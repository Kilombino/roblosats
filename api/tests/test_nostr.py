"""
Unit tests for api/nostr.py — pure-logic, no relay required.

Coverage:
- generate_tags: tag construction and NIP-69 field values
- get_status_tag: NIP-69 statuses (0.9.11: an expired order is "expired", not "success")
- sign_message: Schnorr signature shape, determinism, bad-key fallback

Network tests (send_order_event, send_notification_event, initialize_client)
live in tests/test_nostr_relay.py and run against the real strfry relay in
the docker-tests.yml stack. There are no Client mocks here.
"""

import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase


from api.nostr import Nostr
from api.models import Order

# ---------------------------------------------------------------------------
# A deterministic nsec used only in tests — never a real key.
# Generated with Keys.generate() from nostr-sdk 0.45.0.
TEST_NSEC = "nsec1w72q58pyng0fa8czqeyr4qvw5v58vegxeremqclshlncqns83cpsd2nmk9"


def _make_order(
    *,
    order_id=42,
    order_type=Order.Types.BUY,
    status=Order.Status.PUB,
    has_range=False,
    amount=Decimal("100.00"),
    min_amount=None,
    max_amount=None,
    payment_method="Revolut SEPA",
    premium=Decimal("1.5"),
    bond_size=Decimal("3.0"),
    currency_str="EUR",
    description=None,
    password=None,
    latitude=None,
    longitude=None,
    escrow_duration=3600,
    maker_username="RoboMaker",
    maker_hash_id="abc123",
):
    """Build a fully-specified MagicMock that looks like an Order instance."""
    order = MagicMock()
    order.id = order_id
    order.type = order_type
    order.status = status
    order.has_range = has_range
    order.amount = amount
    order.min_amount = min_amount
    order.max_amount = max_amount
    order.payment_method = payment_method
    order.premium = premium
    order.bond_size = bond_size
    order.currency = MagicMock()
    order.currency.__str__ = lambda self: currency_str
    order.description = description
    order.password = password
    order.latitude = latitude
    order.longitude = longitude
    order.escrow_duration = escrow_duration
    order.expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)

    # maker relationship used by get_user_name / get_robot_hash_id
    order.maker = MagicMock()
    order.maker.username = maker_username
    order.maker.robot = MagicMock()
    order.maker.robot.hash_id = maker_hash_id

    return order


# ---------------------------------------------------------------------------
# Shared env overrides applied to generate_tags tests
ENV_OVERRIDES = {
    "NOSTR_NSEC": TEST_NSEC,
    "COORDINATOR_ALIAS": "TestCoord",
    "HOST_NAME": "test.onion",
    "NETWORK": "testnet",
    "STRFRY_HOST": "localhost",
    "STRFRY_PORT": "7778",
}


# ---------------------------------------------------------------------------
class TestNostrGenerateTags(TestCase):
    """Unit-test the synchronous tag-generation logic."""

    def setUp(self):
        self.nostr = Nostr()

    def _tags_as_dict(self, tags):
        """Convert a list of Tag objects to {tag_name: [values...]} for easy assertions."""
        result = {}
        for tag in tags:
            data = tag.to_vec()  # nostr-sdk 0.45.0: Tag.to_vec() → List[str]
            if data:
                result[data[0]] = data[1:]
        return result

    @patch("api.nostr.config")
    def test_buy_order_tag(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(order_type=Order.Types.BUY)
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        self.assertEqual(self._tags_as_dict(tags)["k"], ["buy"])

    @patch("api.nostr.config")
    def test_sell_order_tag(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(order_type=Order.Types.SELL)
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        self.assertEqual(self._tags_as_dict(tags)["k"], ["sell"])

    @patch("api.nostr.config")
    def test_fixed_amount_fa_tag(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(has_range=False, amount=Decimal("250.00"))
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        self.assertEqual(self._tags_as_dict(tags)["fa"], ["250.00"])

    @patch("api.nostr.config")
    def test_range_amount_fa_tag(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(
            has_range=True,
            min_amount=Decimal("100.00"),
            max_amount=Decimal("500.00"),
        )
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        self.assertEqual(self._tags_as_dict(tags)["fa"], ["100.00", "500.00"])

    @patch("api.nostr.config")
    def test_d_tag_is_deterministic_uuid(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(order_id=42)
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        d_values = self._tags_as_dict(tags)["d"]
        self.assertEqual(len(d_values), 1)
        # Value must be a valid UUID string
        parsed = uuid.UUID(d_values[0])
        # Re-derive the expected value
        hashed_id = hashlib.md5(
            f"{ENV_OVERRIDES['COORDINATOR_ALIAS']}42".encode("utf-8")
        ).hexdigest()
        self.assertEqual(parsed, uuid.UUID(hashed_id))

    @patch("api.nostr.config")
    def test_required_tags_present(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order()
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        tag_dict = self._tags_as_dict(tags)
        for required in (
            "d",
            "name",
            "k",
            "f",
            "s",
            "amt",
            "fa",
            "pm",
            "premium",
            "source",
            "expiration",
            "y",
            "network",
            "layer",
            "bond",
            "z",
        ):
            with self.subTest(tag=required):
                self.assertIn(required, tag_dict)

    @patch("api.nostr.config")
    def test_geo_tag_added_when_coordinates_present(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(latitude=34.7455, longitude=135.503)
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        self.assertIn("g", self._tags_as_dict(tags))

    @patch("api.nostr.config")
    def test_geo_tag_absent_without_coordinates(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(latitude=None, longitude=None)
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        self.assertNotIn("g", self._tags_as_dict(tags))

    @patch("api.nostr.config")
    def test_payment_method_split_into_pm_tag(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(payment_method="Revolut SEPA Wire")
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        self.assertEqual(self._tags_as_dict(tags)["pm"], ["Revolut", "SEPA", "Wire"])

    @patch("api.nostr.config")
    def test_y_tag_contains_coordinator_alias(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order()
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        y_values = self._tags_as_dict(tags)["y"]
        self.assertIn("robosats", y_values)
        self.assertIn(ENV_OVERRIDES["COORDINATOR_ALIAS"].lower(), y_values)

    @patch("api.nostr.config")
    def test_network_tag_value(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order()
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        self.assertEqual(self._tags_as_dict(tags)["network"], ["testnet"])

    @patch("api.nostr.config")
    def test_source_tag_url_format(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(
            key, kw.get("default", "")
        )
        order = _make_order(order_id=99)
        tags = self.nostr.generate_tags(order, "RoboMaker", "abc123", "EUR")
        source_values = self._tags_as_dict(tags)["source"]
        self.assertEqual(len(source_values), 1)
        self.assertIn("test.onion", source_values[0])
        self.assertIn("/99", source_values[0])


# ---------------------------------------------------------------------------
class TestNostrGetStatusTag(TestCase):
    """Unit-test get_status_tag."""

    def setUp(self):
        self.nostr = Nostr()

    def test_pub_status_returns_pending(self):
        order = _make_order(status=Order.Status.PUB)
        self.assertEqual(self.nostr.get_status_tag(order), "pending")

    def test_nip69_statuses(self):
        """0.9.11: an expired order said "success"; each status now has its NIP-69 meaning."""
        S = Order.Status
        want = {
            S.EXP: "expired", S.UCA: "canceled", S.CCA: "canceled", S.PAU: "canceled",
            S.TAK: "in-progress", S.WF2: "in-progress", S.CHA: "in-progress", S.DIS: "in-progress",
            S.SUC: "success", S.MLD: "success", S.TLD: "success",
        }
        for status, tag in want.items():
            with self.subTest(status=status):
                self.assertEqual(self.nostr.get_status_tag(_make_order(status=status)), tag)


class TestNostrClosedOrderExpiration(TestCase):
    """0.9.11 (#28): the event taking an order out of the book carried order.expires_at, already
    past for an expired order; strfry refused it and the old "pending" one stayed listed."""

    def setUp(self):
        self.nostr = Nostr()

    def test_listed_order_keeps_its_expiration(self):
        order = _make_order(status=Order.Status.PUB)
        order.expires_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(self.nostr.expiration_of(order, now=1_800_000_000), int(order.expires_at.timestamp()))

    def test_closed_order_expiration_is_in_the_future(self):
        for status in (Order.Status.EXP, Order.Status.UCA, Order.Status.TAK, Order.Status.SUC):
            with self.subTest(status=status):
                order = _make_order(status=status)
                order.expires_at = datetime(2026, 11, 20, tzinfo=timezone.utc)  # long past
                now = int(datetime(2026, 11, 21, tzinfo=timezone.utc).timestamp())
                self.assertEqual(self.nostr.expiration_of(order, now=now), now + 3600)

    @patch("api.nostr.config")
    def test_expiration_tag_and_deletion_coordinate(self, mock_config):
        mock_config.side_effect = lambda key, **kw: ENV_OVERRIDES.get(key, kw.get("default", ""))
        order = _make_order(status=Order.Status.EXP)
        order.expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        tags = {t.to_vec()[0]: t.to_vec() for t in self.nostr.generate_tags(order, "R", "h", "EUR")}
        import time

        self.assertGreater(int(tags["expiration"][1]), time.time() + 3000)
        self.assertEqual(tags["s"][1], "expired")
        dele = [t.to_vec() for t in self.nostr.deletion_tags(order, "ab" * 32)]
        self.assertEqual(dele[0], ["a", f"38383:{'ab' * 32}:{tags['d'][1]}"])
        self.assertEqual(dele[1], ["k", "38383"])


# ---------------------------------------------------------------------------
class TestNostrSignMessage(TestCase):
    """Unit-test the static sign_message helper."""

    @patch("api.nostr.config")
    def test_sign_message_returns_hex_string(self, mock_config):
        mock_config.return_value = TEST_NSEC
        result = Nostr.sign_message("test message")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)
        # Schnorr signatures are 64 bytes = 128 hex chars
        self.assertEqual(len(result), 128)

    @patch("api.nostr.config")
    def test_sign_message_is_deterministic_for_same_input(self, mock_config):
        # Note: secp256k1 Schnorr with raw=True is deterministic for same key+msg
        mock_config.return_value = TEST_NSEC
        sig1 = Nostr.sign_message("hello robosats")
        sig2 = Nostr.sign_message("hello robosats")
        self.assertEqual(sig1, sig2)

    @patch("api.nostr.config")
    def test_sign_message_differs_for_different_inputs(self, mock_config):
        mock_config.return_value = TEST_NSEC
        sig1 = Nostr.sign_message("message A")
        sig2 = Nostr.sign_message("message B")
        self.assertNotEqual(sig1, sig2)

    @patch("api.nostr.config")
    def test_sign_message_returns_empty_on_bad_key(self, mock_config):
        mock_config.return_value = "not_a_valid_nsec"
        result = Nostr.sign_message("any text")
        self.assertEqual(result, "")
