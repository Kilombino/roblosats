package com.robosats.models

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NotificationDedupTest {
    private class MemStore : NotificationDedup.Store {
        val m = HashMap<String, String>()
        override fun get(key: String) = m[key] ?: ""
        override fun set(key: String, value: String) { m[key] = value }
    }

    @Test
    fun sameNoticeAfterARestartIsNotShownAgain() {
        // the bug of 0.9.4: "Order 24 finished" came back long after it was shown
        val store = MemStore()
        assertTrue(NotificationDedup.shouldNotify(store, "finished24", 1_000))
        // the service restarts (its in-memory set is gone) and the relay sends the same gift wrap
        assertFalse(NotificationDedup.shouldNotify(store, "finished24", 1_000))
        assertFalse(NotificationDedup.shouldNotify(store, "finished24", 1_000))
    }

    @Test
    fun twoNoticesInTheSameSecondBothShow() {
        val store = MemStore()
        assertTrue(NotificationDedup.shouldNotify(store, "a", 2_000))
        assertTrue(NotificationDedup.shouldNotify(store, "b", 2_000))
        assertFalse(NotificationDedup.shouldNotify(store, "a", 2_000))
    }

    @Test
    fun anOlderNoticeTheRelayStillKeepsIsNotShown() {
        val store = MemStore()
        assertTrue(NotificationDedup.shouldNotify(store, "new", 3_000))
        assertFalse(NotificationDedup.shouldNotify(store, "old", 2_999))
        assertEquals("3000", store.get(NotificationDedup.KEY_LAST))
    }

    @Test
    fun theWebSideLastNotificationDoesNotHideANewNoticeOfThatSecond() {
        // the in-app web writes last_notification when it shows a notice in its own drawer
        val store = MemStore()
        store.set(NotificationDedup.KEY_LAST, "4000")
        assertTrue(NotificationDedup.shouldNotify(store, "c", 4_000))
        assertFalse(NotificationDedup.shouldNotify(store, "c", 4_000))
    }

    @Test
    fun theSeenListIsBounded() {
        val store = MemStore()
        for (i in 0 until NotificationDedup.MAX_SEEN + 50) {
            assertTrue(NotificationDedup.shouldNotify(store, "id$i", 10_000L + i))
        }
        val seen = NotificationDedup.parse(store.get(NotificationDedup.KEY_SEEN))
        assertEquals(NotificationDedup.MAX_SEEN, seen.size)
        assertTrue(seen.containsKey("id${NotificationDedup.MAX_SEEN + 49}"))
        assertFalse(seen.containsKey("id0"))
    }

    @Test
    fun parseIgnoresGarbage() {
        val m = NotificationDedup.parse("x:1;bad;y:notanumber;z:3")
        assertEquals(mapOf("x" to 1L, "z" to 3L), m)
    }
}
