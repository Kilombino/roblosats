package com.robosats.models

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** 0.9.13: crash "secret key must be 32 bytes" in the notification service (0.9.12). */
class RobotKeysTest {
    private val pubA = "a".repeat(64)
    private val pubB = "b".repeat(64)
    private val secret = ByteArray(32) { (it * 7 + 1).toByte() }

    /** As the page stores a Uint8Array: {"0": n, "1": n, …}. */
    private fun u8(bytes: ByteArray) =
        bytes.withIndex().joinToString(",", "{", "}") { (i, b) -> "\"$i\":${b.toInt() and 0xff}" }

    private fun slot(pub: String, secJson: String?) =
        "{\"token\":\"t\",\"nostrPubKey\":\"$pub\"" + (secJson?.let { ",\"nostrSecKey\":$it" } ?: "") + "}"

    @Test
    fun readsTheStoredUint8Array() {
        val garage = "{\"t1\":${slot(pubA, u8(secret))}}"
        assertArrayEquals(secret, RobotKeys.secretFor(garage, pubA))
        assertEquals(listOf(pubA), RobotKeys.pubKeys(garage))
    }

    @Test
    fun emptyOrPartialGarageGivesNoKeyInsteadOfAnEmptyOne() {
        // the 0.9.12 crash: the robot list came from the full garage, the key from "{}"
        assertNull(RobotKeys.secretFor("{}", pubA))
        assertNull(RobotKeys.secretFor("", pubA))
        assertNull(RobotKeys.secretFor("{\"t2\":${slot(pubB, u8(secret))}}", pubA))
    }

    @Test
    fun badSecretsAreRejected() {
        val bad = listOf(
            "{}",
            u8(ByteArray(31)),
            u8(ByteArray(33)),
            "{\"0\":256" + (1 until 32).joinToString("") { ",\"$it\":1" } + "}",
            "{\"0\":-1" + (1 until 32).joinToString("") { ",\"$it\":1" } + "}",
            // 32 entries but a gap in the indexes
            "{\"1\":1" + (2 until 33).joinToString("") { ",\"$it\":1" } + "}",
            "\"\"",
            "\"nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\"",
            "\"" + "ab".repeat(31) + "\"",
            "\"AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA=\"",
            "null",
            "[1,2,3]",
            "true",
        )
        for (b in bad) {
            assertNull("accepted $b", RobotKeys.secretFor("{\"t\":${slot(pubA, b)}}", pubA))
        }
        // a slot without nostrSecKey at all
        assertNull(RobotKeys.secretFor("{\"t\":${slot(pubA, null)}}", pubA))
    }

    @Test
    fun arrayAndHexFormsAreAccepted() {
        val arr = secret.joinToString(",", "[", "]") { (it.toInt() and 0xff).toString() }
        assertArrayEquals(secret, RobotKeys.secretFor("{\"t\":${slot(pubA, arr)}}", pubA))
        val hex = "\"" + RobotKeys.toHex(secret) + "\""
        assertArrayEquals(secret, RobotKeys.secretFor("{\"t\":${slot(pubA, hex)}}", pubA))
    }

    @Test
    fun oneBadRobotDoesNotHideTheOthers() {
        val garage = "{" +
            "\"bad\":${slot(pubA, "{}")}," +
            "\"notAnObject\":\"x\"," +
            "\"noPub\":{\"nostrSecKey\":${u8(secret)}}," +
            "\"badPub\":${slot("xyz", u8(secret))}," +
            "\"good\":${slot(pubB, u8(secret))}" +
            "}"
        assertEquals(listOf(pubA, pubB), RobotKeys.pubKeys(garage))
        assertNull(RobotKeys.secretFor(garage, pubA))
        assertArrayEquals(secret, RobotKeys.secretFor(garage, pubB))
    }

    @Test
    fun unreadableGarageIsEmpty() {
        assertTrue(RobotKeys.slots("{not json").isEmpty())
        assertTrue(RobotKeys.slots("[]").isEmpty())
        assertNull(RobotKeys.secretFor("{not json", pubA))
        assertNull(RobotKeys.secretFor("{}", "not-a-pubkey"))
    }

    @Test
    fun pubkeyCaseIsNormalised() {
        val garage = "{\"t\":${slot(pubA.uppercase(), u8(secret))}}"
        assertArrayEquals(secret, RobotKeys.secretFor(garage, pubA.uppercase()))
        assertEquals(listOf(pubA), RobotKeys.pubKeys(garage))
    }
}
