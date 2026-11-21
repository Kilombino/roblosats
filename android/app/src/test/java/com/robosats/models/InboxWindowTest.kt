package com.robosats.models

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class InboxWindowTest {
    private val now = 1_800_000_000L
    private val day = 24 * 3600L

    @Test
    fun firstRunLooksBackThreeDays() {
        assertEquals(now - 3 * day, InboxWindow.since(now, null))
        assertEquals(now - 3 * day, InboxWindow.since(now, 0))
        // a clock from the future is ignored
        assertEquals(now - 3 * day, InboxWindow.since(now, now + day))
    }

    @Test
    fun coversTheGiftWrapJitterBeforeTheLastActiveTime() {
        val last = now - 600
        assertEquals(last - InboxWindow.JITTER_SECONDS, InboxWindow.since(now, last))
        assertTrue(InboxWindow.since(now, last) <= last - 2 * day)
    }

    @Test
    fun neverMoreThanSevenDaysBack() {
        // the 0.9.12 flood: 10 days of gift wraps on every start
        assertEquals(now - 7 * day, InboxWindow.since(now, now - 30 * day))
    }

    @Test
    fun seenWrapsSurviveARestartAndAreCapped() {
        val a = SeenWraps("")
        assertTrue(a.firstTime("w1", 10))
        assertFalse(a.firstTime("w1", 10))
        assertTrue(a.dirty)
        val raw = a.serialize()
        assertFalse(a.dirty)
        val b = SeenWraps(raw)
        assertFalse(b.firstTime("w1", 10))
        assertTrue(b.firstTime("w2", 11))

        val c = SeenWraps("", max = 3)
        for (i in 1..5) c.firstTime("w$i", i.toLong())
        val kept = SeenWraps(c.serialize())
        assertEquals(3, kept.size())
        assertFalse(kept.firstTime("w5", 5))
        assertTrue(kept.firstTime("w1", 1))
    }
}
