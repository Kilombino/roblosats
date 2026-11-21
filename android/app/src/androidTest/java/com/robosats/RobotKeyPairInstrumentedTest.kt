package com.robosats

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.robosats.models.EncryptedStorage
import com.robosats.models.NostrClient
import com.robosats.models.RobotKeys
import com.vitorpamplona.quartz.crypto.KeyPair
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/** 0.9.13: getRobotKeyPair with the real secp256k1: bad keys give null, never an exception. */
@RunWith(AndroidJUnit4::class)
class RobotKeyPairInstrumentedTest {
    private val secret = ByteArray(32) { (it + 1).toByte() }
    private val pub = RobotKeys.toHex(KeyPair(secret).pubKey)

    @Before
    fun storage() {
        // NostrClient reads the garage when it is first loaded
        EncryptedStorage.init(InstrumentationRegistry.getInstrumentation().targetContext)
    }

    private fun u8(bytes: ByteArray) =
        bytes.withIndex().joinToString(",", "{", "}") { (i, b) -> "\"$i\":${b.toInt() and 0xff}" }

    private fun garage(p: String, sec: String) = "{\"t\":{\"nostrPubKey\":\"$p\",\"nostrSecKey\":$sec}}"

    @Test
    fun goodRobot() {
        val kp = NostrClient.getRobotKeyPair(pub, garage(pub, u8(secret)))
        assertNotNull(kp)
        assertArrayEquals(secret, kp!!.privKey)
    }

    @Test
    fun badRobotsAreSkipped() {
        // the 0.9.12 crash: an empty garage snapshot
        assertNull(NostrClient.getRobotKeyPair(pub, "{}"))
        assertNull(NostrClient.getRobotKeyPair(pub, ""))
        assertNull(NostrClient.getRobotKeyPair(pub, garage(pub, "{}")))
        assertNull(NostrClient.getRobotKeyPair(pub, garage(pub, u8(ByteArray(31)))))
        // all-zero is not a valid secp256k1 secret
        assertNull(NostrClient.getRobotKeyPair(pub, garage(pub, u8(ByteArray(32)))))
        // a key that is not the one of this pubkey
        val other = ByteArray(32) { 9 }
        assertNull(NostrClient.getRobotKeyPair(pub, garage(pub, u8(other))))
    }
}
