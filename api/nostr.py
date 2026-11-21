from decimal import Decimal
import pygeohash
import hashlib
import uuid

from secp256k1 import PrivateKey
from asgiref.sync import sync_to_async
from nostr_sdk import (
    Keys,
    Client,
    EventBuilder,
    Kind,
    Tag,
    PublicKey,
    RelayUrl,
    nip17_make_private_msg_async,
)
from api.models import Order
from api.utils import get_federation_short_alias
from decouple import config


class Nostr:
    """Simple nostr events manager to be used as a cache system for clients"""

    async def send_order_event(self, order):
        """Creates the event and sends it to the coordinator relay"""

        # Publish only public orders
        if order.password is not None:
            return

        # Onchain escrow orders are not announced while it is open to some robots only
        if order.escrow_type == Order.EscrowTypes.ONCHAIN:
            from api.escrow.flow import restricted

            if restricted():
                return

        if config("NOSTR_NSEC", cast=str, default="") == "":
            return

        print("Sending nostr ORDER event")

        keys = Keys.parse(config("NOSTR_NSEC", cast=str))
        client = await self.initialize_client()

        robot_name = await self.get_user_name(order)
        robot_hash_id = await self.get_robot_hash_id(order)
        currency = await self.get_robot_currency(order)
        pricing = await self.get_pricing(order)

        content = order.description if order.description is not None else ""

        # Keys implements AsyncNostrSigner directly in nostr-sdk 0.45.0
        event = await (
            EventBuilder(Kind(38383), content)
            .tags(self.generate_tags(order, robot_name, robot_hash_id, currency, pricing))
            .finalize_async(keys)
        )
        await client.send_event(event)
        print(f"Nostr ORDER event sent: {event.as_json()}")

        # 0.9.11: an order that left the book is also deleted by its coordinate (NIP-09), so
        # relays that missed or refused the update (an expiration already past, #28) drop it
        if not self.is_listed(order):
            deletion = await (
                EventBuilder(Kind(5), "order no longer in the book")
                .tags(self.deletion_tags(order, keys.public_key().to_hex()))
                .finalize_async(keys)
            )
            await client.send_event(deletion)
            print(f"Nostr ORDER deletion sent: {deletion.as_json()}")

    async def send_notification_event(self, robot, order, text, action=False):
        """Creates the notification event and sends it to the coordinator relay"""
        if config("NOSTR_NSEC", cast=str, default="") == "":
            return

        print("Sending nostr NOTIFICATION event")

        keys = Keys.parse(config("NOSTR_NSEC", cast=str))
        client = await self.initialize_client()

        rumor_extra_tags = [
            Tag.parse(
                [
                    "order_id",
                    f"{get_federation_short_alias()}/{order.id}",
                ]
            ),
            Tag.parse(["status", str(order.status)]),
        ]
        if action:  # 0.9.4: the trader must do something (Android "Order needs you" channel)
            rumor_extra_tags.append(Tag.parse(["action", "1"]))

        # Keys implements AsyncNostrSigner directly in nostr-sdk 0.45.0
        gift_wrap = await nip17_make_private_msg_async(
            keys,
            PublicKey.parse(robot.nostr_pubkey),
            text,
            rumor_extra_tags=rumor_extra_tags,
        )
        await client.send_event(gift_wrap)
        print("Nostr NOTIFICATION event sent")

    async def initialize_client(self):
        # Initialize a bare client (no signer needed on the client itself)
        client = Client()

        # Add relays and connect
        strfry_host = config("STRFRY_HOST", cast=str, default="localhost")
        strfry_port = config("STRFRY_PORT", cast=str, default="7778")
        await client.add_relay(RelayUrl.parse(f"ws://{strfry_host}:{strfry_port}"))
        await client.connect()

        return client

    @sync_to_async
    def get_user_name(self, order):
        return order.maker.username

    @sync_to_async
    def get_robot_hash_id(self, order):
        return order.maker.robot.hash_id

    @sync_to_async
    def get_robot_currency(self, order):
        return str(order.currency)

    @sync_to_async
    def get_pricing(self, order):
        return self.pricing_tags(order)

    @staticmethod
    def pricing_tags(order):
        """NIP-69 pricing (rl31-beta17). A market order: amt 0 + its premium. A fixed-price
        order with one amount: amt = its sats (fa + amt fix the price, as the spec reads a
        non-zero amt); with a range the sats depend on the amount taken, so amt stays 0. In
        both, premium is the fixed price against the market now (0 without a market price)
        and an extra ["price", <fixed price per BTC>] tag carries the price itself."""
        from api.logics import Logics

        if getattr(order, "sats_denominated", False) is True:
            return Nostr.sats_pricing_tags(order)
        if not Logics.is_fixed_price(order):
            return {"amt": "0", "premium": str(order.premium), "price": None}
        premium = Logics.fixed_price_premium(order)
        amt = "0" if order.has_range else str(int(Logics.satoshis_now(order)))
        return {
            "amt": amt,
            "premium": str(premium if premium is not None else 0),
            "price": format(order.fixed_price.normalize(), "f")
            if hasattr(order.fixed_price, "normalize")
            else str(order.fixed_price),
        }

    @staticmethod
    def sats_pricing_tags(order):
        """A "丰 sat" order (rl31-beta19): the size is in sats and the currency is what is paid
        in exchange. So that every NIP-69 client reads it right:
        - amt: the sats (one amount); a range: 0 (NIP-69 has one amt);
        - fa: what is paid in exchange, in the currency (a range: for the min and the max sats),
          at the fixed price, or at market + premium when published;
        - premium: the fixed price against the market (0 without one), or the order's premium;
        - price: the fixed price per BTC (only fixed-price orders);
        - ["sats", <sats>] or ["sats", <min>, <max>]: the size in sats, which marks the order as
          a sat order for our app."""
        from api.logics import Logics

        sizes = [order.amount] if not order.has_range else [order.min_amount, order.max_amount]
        totals = [Logics.exchange_total(order, s) for s in sizes]
        fa = [("0" if t is None else f"{t:.8f}".rstrip("0").rstrip(".")) for t in totals]
        sats = [str(int(Decimal(str(s)))) for s in sizes]
        if Logics.is_fixed_price(order):
            premium = Logics.fixed_price_premium(order)
            price = format(order.fixed_price.normalize(), "f") if hasattr(order.fixed_price, "normalize") \
                else str(order.fixed_price)
        else:
            premium, price = order.premium, None
        return {
            "amt": "0" if order.has_range else sats[0],
            "premium": str(premium if premium is not None else 0),
            "price": price,
            "fa": fa,
            "sats": sats,
        }

    # 0.9.11: the event that takes an order out of the book must outlive its publication. It
    # carried order.expires_at, already past for an expired order, and strfry refused it (NIP-40)
    # in silence: the old "pending" event stayed listed until its own expiration (#28).
    CLOSED_EVENT_TTL = 3600

    @staticmethod
    def is_listed(order):
        return order.status == Order.Status.PUB

    @staticmethod
    def d_tag(order):
        hashed_id = hashlib.md5(
            f"{config('COORDINATOR_ALIAS', cast=str)}{order.id}".encode("utf-8")
        ).hexdigest()
        return str(uuid.UUID(hashed_id))

    def expiration_of(self, order, now=None):
        """NIP-40 expiration: the order's own while it is listed; once it left the book, never
        less than an hour from now (the replacement must be accepted, then it may go)."""
        import time

        now = int(now if now is not None else time.time())
        expires = int(order.expires_at.timestamp()) if order.expires_at else now
        if self.is_listed(order):
            return expires
        ttl = config("NOSTR_CLOSED_EVENT_TTL", cast=int, default=self.CLOSED_EVENT_TTL)
        return max(expires, now + max(60, ttl))

    def deletion_tags(self, order, pubkey_hex):
        return [
            Tag.parse(["a", f"38383:{pubkey_hex}:{self.d_tag(order)}"]),
            Tag.parse(["k", "38383"]),
        ]

    def generate_tags(self, order, robot_name, robot_hash_id, currency, pricing=None):

        if pricing is None:
            pricing = self.pricing_tags(order)
        tags = [
            Tag.parse(["d", self.d_tag(order)]),
            Tag.parse(["name", robot_name, robot_hash_id]),
            Tag.parse(["k", "sell" if order.type == Order.Types.SELL else "buy"]),
            Tag.parse(["f", currency]),
            Tag.parse(["s", self.get_status_tag(order)]),
            Tag.parse(["amt", pricing["amt"]]),
            Tag.parse(
                ["fa"]
                + (
                    pricing["fa"]
                    if pricing.get("fa")
                    else [str(order.amount)]
                    if not order.has_range
                    else [str(order.min_amount), str(order.max_amount)]
                )
            ),
            Tag.parse(["pm"] + order.payment_method.split(" ")),
            Tag.parse(["premium", pricing["premium"]]),
            Tag.parse(
                [
                    "source",
                    f"http://{config('HOST_NAME')}/order/{get_federation_short_alias()}/{order.id}",
                ]
            ),
            Tag.parse(
                [
                    "expiration",
                    str(self.expiration_of(order)),
                    str(order.escrow_duration),
                ]
            ),
            Tag.parse(["y", "robosats", config("COORDINATOR_ALIAS", cast=str).lower()]),
            Tag.parse(["network", str(config("NETWORK"))]),
            Tag.parse(["layer", "onchain" if order.escrow_type == Order.EscrowTypes.ONCHAIN else "lightning"]),
            Tag.parse(["bond", str(order.bond_size)]),
            Tag.parse(["z", "order"]),
        ]

        if pricing.get("price"):
            tags.append(Tag.parse(["price", pricing["price"]]))
        if pricing.get("sats"):
            tags.append(Tag.parse(["sats"] + pricing["sats"]))

        if order.latitude and order.longitude:
            tags.extend(
                [Tag.parse(["g", pygeohash.encode(order.latitude, order.longitude)])]
            )

        return tags

    def get_status_tag(self, order):
        """NIP-69 status. Until 0.9.11 everything but PUB was "success" (an expired order too)."""
        S = Order.Status
        if order.status == S.PUB:
            return "pending"
        if order.status == S.EXP:
            return "expired"
        if order.status in (S.UCA, S.CCA, S.PAU, S.WFB):
            return "canceled"
        if order.status in (S.SUC, S.MLD, S.TLD):
            return "success"
        return "in-progress"

    def sign_message(text: str) -> str:
        try:
            keys = Keys.parse(config("NOSTR_NSEC", cast=str))
            secret_key_hex = keys.secret_key().to_hex()
            private_key = PrivateKey(bytes.fromhex(secret_key_hex))
            signature = private_key.schnorr_sign(
                text.encode("utf-8"), bip340tag=None, raw=True
            )

            return signature.hex()
        except Exception:
            return ""
