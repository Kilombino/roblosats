"""
Roblosats rl25: the chat accepts the offset that 0.8.8 clients send in Nostr mode.

In Nostr mode the 0.8.8 app sends the Nostr timestamp of its last message as
`offset`, with decimals. /api/chat/ only took integers, so from the second message
on the coordinator's copy was rejected: the peer on the API never saw it, and in a
dispute the coordinator had no chat (and could even solve it against that robot).
"""

from django.test import SimpleTestCase

from chat.serializers import PostMessageSerializer
from chat.views import message_offset


class ChatOffsetTest(SimpleTestCase):
    def test_timestamp_with_decimals_is_accepted(self):
        s = PostMessageSerializer(
            data={"PGP_message": "x", "order_id": 149, "offset": 1793650000.0004}
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_integer_and_missing_offsets_still_work(self):
        for data in ({"offset": 3}, {"offset": None}, {}):
            s = PostMessageSerializer(data={"PGP_message": "x", "order_id": 1, **data})
            self.assertTrue(s.is_valid(), s.errors)

    def test_message_offset(self):
        self.assertEqual(message_offset("1793650000.0004"), 1793650000)
        self.assertEqual(message_offset(4), 4)
        self.assertEqual(message_offset("7"), 7)
        self.assertEqual(message_offset("abc"), 0)
        self.assertEqual(message_offset(None), 0)
        self.assertEqual(message_offset(-3), 0)
        self.assertEqual(message_offset("inf"), 0)
