package com.robosats.updates

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ReleaseRulesTest {
    private val names = listOf(
        "roblosats-v0.9.6-arm64-v8a.apk",
        "roblosats-v0.9.6-armeabi-v7a.apk",
        "roblosats-v0.9.6-universal.apk",
        "roblosats-v0.9.6-x86_64.apk",
        "roblosats-desktop-0.9.6-linux-x64.zip",
        "SHA256SUMS",
    )

    // the notes of a real release (0.9.5): the hashes go in a "### SHA-256" block
    private val body = """
        ## Roblosats 0.9.6
        ### SHA-256
        ```
        653fd4eb382e1c192993e10cb219df03fcc01d6121412d0611f986fab50a1fd6  roblosats-v0.9.6-arm64-v8a.apk
        7ca50acbe182cd841912dd4334bc15ff69d8c97a89f831f6a5c2a47bcd4b2dd2  roblosats-v0.9.6-armeabi-v7a.apk
        5e6de6a0d32cbb924d6ab1897dfa63917a538e1905cd43d8883c1baed168a263  roblosats-v0.9.6-universal.apk
        6a2fb869d87877886c6ab0dee5cccd5c8625349a5d4518cf54d2ba246a4a5133  roblosats-v0.9.6-x86_64.apk
        ```
    """.trimIndent()

    @Test
    fun versions() {
        assertTrue(ReleaseRules.isNewer("v0.9.6", "0.9.5"))
        assertFalse(ReleaseRules.isNewer("v0.9.5", "0.9.5"))
        assertTrue(ReleaseRules.isNewer("0.9.6", "0.9.6-beta1"))
        assertFalse(ReleaseRules.isNewer("0.9.6-beta1", "0.9.6"))
        assertTrue(ReleaseRules.isNewer("0.10.0", "0.9.9"))
    }

    @Test
    fun theSplitForThePhonesAbi() {
        assertEquals("roblosats-v0.9.6-arm64-v8a.apk", ReleaseRules.pickApk(names, listOf("arm64-v8a", "armeabi-v7a", "armeabi")))
        assertEquals("roblosats-v0.9.6-armeabi-v7a.apk", ReleaseRules.pickApk(names, listOf("armeabi-v7a", "armeabi")))
        assertEquals("roblosats-v0.9.6-x86_64.apk", ReleaseRules.pickApk(names, listOf("x86_64", "arm64-v8a")))
        // an x86 (32-bit) phone: no split for it, the universal APK
        assertEquals("roblosats-v0.9.6-universal.apk", ReleaseRules.pickApk(names, listOf("x86")))
        assertNull(ReleaseRules.pickApk(listOf("SHA256SUMS"), listOf("arm64-v8a")))
    }

    @Test
    fun theHashOfThatFileFromTheNotes() {
        assertEquals(
            "653fd4eb382e1c192993e10cb219df03fcc01d6121412d0611f986fab50a1fd6",
            ReleaseRules.hashFor(body, "roblosats-v0.9.6-arm64-v8a.apk"),
        )
        assertEquals(
            "6a2fb869d87877886c6ab0dee5cccd5c8625349a5d4518cf54d2ba246a4a5133",
            ReleaseRules.hashFor(body, "roblosats-v0.9.6-x86_64.apk"),
        )
        // notes without the hash: only the page is offered
        assertNull(ReleaseRules.hashFor("## no hashes here", "roblosats-v0.9.6-arm64-v8a.apk"))
    }

    @Test
    fun aSingleSignedApkLineAlsoCounts() {
        val h = "a".repeat(64)
        assertEquals(h, ReleaseRules.hashFor("Signed APK SHA-256: `$h`", "roblosats-v0.9.6-universal.apk"))
        // two such lines: ambiguous, not trusted
        assertNull(ReleaseRules.hashFor("Signed APK SHA-256: `$h`\nSigned APK SHA-256: `${"b".repeat(64)}`", "x.apk"))
    }
}
