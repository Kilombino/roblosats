package com.robosats.models

import org.json.JSONArray
import org.json.JSONObject

/**
 * 0.9.13: the robots' nostr keys as the page stores them in "garage_slots", read without ever
 * throwing. The notification service used to read the garage twice per gift wrap (once for the
 * list of robots, once for the key) and to hand whatever it found to secp256k1: when the page
 * rewrote the garage in between (it saves it once per robot while it loads it at start-up, so
 * for a moment it is "{}" or has only some of the robots), the key came back empty and
 * `KeyPair` threw "secret key must be 32 bytes" in a relay coroutine, which killed the app.
 *
 * Here: one snapshot of the garage, each slot checked on its own (a bad one is skipped, the rest
 * still work), and only well-formed values come out: a 64-hex lowercase pubkey and a 32-byte
 * secret. Pure JVM (org.json), unit-tested.
 */
object RobotKeys {
    const val KEY_BYTES = 32
    private val HEX64 = Regex("^[0-9a-f]{64}$")

    fun isHexKey(s: String?): Boolean = s != null && HEX64.matches(s)

    /**
     * The 32 bytes of a stored secret. The page stores a Uint8Array, which JSON.stringify writes
     * as {"0": 12, "1": 200, …}; an array of numbers or a 64-hex string are accepted too. Null for
     * anything else (missing, empty, wrong length, values out of 0..255, gaps in the indexes).
     */
    fun secretBytes(raw: Any?): ByteArray? {
        return try {
            when (raw) {
                is JSONObject -> {
                    if (raw.length() != KEY_BYTES) return null
                    ByteArray(KEY_BYTES) { i -> byteOf(raw.opt(i.toString())) ?: return null }
                }
                is JSONArray -> {
                    if (raw.length() != KEY_BYTES) return null
                    ByteArray(KEY_BYTES) { i -> byteOf(raw.opt(i)) ?: return null }
                }
                is String -> {
                    val s = raw.trim().lowercase()
                    if (!isHexKey(s)) return null
                    ByteArray(KEY_BYTES) { i -> s.substring(2 * i, 2 * i + 2).toInt(16).toByte() }
                }
                else -> null
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun byteOf(v: Any?): Byte? {
        val n = when (v) {
            is Int -> v
            is Long -> if (v in 0L..255L) v.toInt() else return null
            is Double -> if (v % 1.0 == 0.0) v.toInt() else return null
            is String -> v.toIntOrNull() ?: return null
            else -> return null
        }
        return if (n in 0..255) n.toByte() else null
    }

    /** One robot of the garage: its pubkey and, when the stored one is well formed, its secret. */
    class Slot(val pubKey: String, val secret: ByteArray?)

    /**
     * Every slot of a garage dump with a valid pubkey; a slot that is not an object or has no
     * valid pubkey is left out, one with a bad secret comes with `secret = null`. Empty for an
     * empty or unreadable dump.
     */
    fun slots(garageString: String): List<Slot> {
        if (garageString.isBlank()) return emptyList()
        val garage = try {
            JSONObject(garageString)
        } catch (e: Exception) {
            return emptyList()
        }
        val out = ArrayList<Slot>()
        val keys = garage.keys()
        while (keys.hasNext()) {
            val slot = garage.optJSONObject(keys.next()) ?: continue
            val pub = slot.optString("nostrPubKey", "").trim().lowercase()
            if (!isHexKey(pub)) continue
            out.add(Slot(pub, secretBytes(slot.opt("nostrSecKey"))))
        }
        return out
    }

    fun pubKeys(garageString: String): List<String> = slots(garageString).map { it.pubKey }.distinct()

    /** The secret of [hexPubKey] in the dump; null when that robot is not there or its key is bad. */
    fun secretFor(garageString: String, hexPubKey: String): ByteArray? {
        val want = hexPubKey.trim().lowercase()
        if (!isHexKey(want)) return null
        return slots(garageString).firstOrNull { it.pubKey == want && it.secret != null }?.secret
    }

    fun toHex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }
}
