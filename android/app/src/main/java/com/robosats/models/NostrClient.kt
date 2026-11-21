package com.robosats.models

import android.util.Log
import com.vitorpamplona.ammolite.relays.COMMON_FEED_TYPES
import com.vitorpamplona.ammolite.relays.Client
import com.vitorpamplona.ammolite.relays.Relay
import com.vitorpamplona.ammolite.relays.RelayPool
import com.vitorpamplona.ammolite.relays.TypedFilter
import com.vitorpamplona.ammolite.relays.filters.FilterSerializer
import com.vitorpamplona.ammolite.relays.filters.IPerRelayFilter
import com.vitorpamplona.quartz.crypto.KeyPair
import com.vitorpamplona.quartz.events.Event
import org.json.JSONArray

object NostrClient {
    private var subscriptionNotificationId = "robosatsNotificationId"
    private var authors = garagePubKeys()
    private var initialized = false

    fun init() {
        if (!initialized) {
            try {
                RelayPool.register(Client)
                initialized = true
            } catch (e: Exception) {
                Log.e("NostrClient", "Error initializing NostrClient: ${e.message}", e)
                // Don't set initialized to true if there was an error
            }
        }
    }

    fun stop() {
        RelayPool.unloadRelays()
    }

    fun start() {
        connectRelays()
        subscribeToInbox()
    }

    fun refresh() {
        val pubKeys = garagePubKeys()
        // 0.9.13: only a garage that gained robots resubscribes. The page rewrites the garage
        // robot by robot while it loads (for a moment "{}", then 1, 2… robots): each of those
        // writes used to send a new REQ and the relay sent the whole history again.
        if (pubKeys.isNotEmpty() && !authors.toSet().containsAll(pubKeys)) {
            subscribeToInbox()
        } else if (pubKeys.isNotEmpty() && pubKeys.toSet() != authors.toSet()) {
            // robots removed: the next subscription (restart, network change) drops them
            Log.d("RobosatsNostrClient", "Garage has fewer robots (${pubKeys.size}/${authors.size}); subscription kept")
        }
    }

    /** 0.9.13: while a relay is connected, remember that the service was listening (see InboxWindow). */
    fun markActive(nowSeconds: Long = System.currentTimeMillis() / 1000) {
        if (RelayPool.getAll().any { it.isConnected() }) {
            EncryptedStorage.setEncryptedStorage(InboxWindow.KEY_LAST_ACTIVE, nowSeconds.toString())
        }
    }

    fun checkRelaysHealth() {
        if (RelayPool.getAll().isEmpty()) {
            stop()
            start()
        }
        RelayPool.getAll().forEach {
            if (!it.isConnected()) {
                Log.d(
                    "RobosatsNostrClient",
                    "Relay ${it.url} is not connected, reconnecting...",
                )
                it.connectAndSendFiltersIfDisconnected()
            }
        }
    }

    fun garagePubKeys(): List<String> =
        RobotKeys.pubKeys(EncryptedStorage.getEncryptedStorage("garage_slots"))

    /**
     * 0.9.13: the key pair of the robot [hexPubKey], or null (logged) when it is not in the
     * garage or its stored key is not a valid 32-byte secret for that pubkey. Never throws: up to
     * 0.9.12 an empty or partial garage (the page rewrites it robot by robot while it loads)
     * reached secp256k1 as an empty key and the exception killed the app.
     *
     * [garageString] lets the caller use the same snapshot it took the robot list from.
     */
    fun getRobotKeyPair(
        hexPubKey: String,
        garageString: String = EncryptedStorage.getEncryptedStorage("garage_slots"),
    ): KeyPair? {
        val secret = RobotKeys.secretFor(garageString, hexPubKey)
        if (secret == null) {
            Log.w("RobosatsNostrClient", "No valid nostr key in the garage for robot ${hexPubKey.take(8)}…; skipped")
            return null
        }
        return try {
            val pair = KeyPair(secret)
            val derived = pair.pubKey.let { RobotKeys.toHex(it) }
            if (derived != hexPubKey.lowercase()) {
                Log.w("RobosatsNostrClient", "Stored nostr key of robot ${hexPubKey.take(8)}… does not match its pubkey; skipped")
                null
            } else {
                pair
            }
        } catch (e: Throwable) {
            Log.w("RobosatsNostrClient", "Invalid nostr key for robot ${hexPubKey.take(8)}…: ${e.message}; skipped")
            null
        }
    }

    private fun connectRelays() {
        val federationRelays = EncryptedStorage.getEncryptedStorage("federation_relays")

        if (federationRelays.isNotEmpty()) {
            val relaysUrls = JSONArray(federationRelays)

            val relayList = (0 until relaysUrls.length()).map { relaysUrls.getString(it) }
            val randomRelays = relayList.shuffled().take(3)

            for (url in randomRelays) {
                Client.sendFilterOnlyIfDisconnected()
                if (RelayPool.getRelays(url).isEmpty()) {
                    RelayPool.addRelay(
                        Relay(
                            url,
                            read = true,
                            write = false,
                            forceProxy = true,
                            activeTypes = COMMON_FEED_TYPES,
                        ),
                    )
                }
            }
        }

    }

    private fun subscribeToInbox() {
        val pubKeys = garagePubKeys()
        if (pubKeys.isEmpty()) return
        authors = pubKeys

        val now = System.currentTimeMillis() / 1000
        val lastActive = InboxWindow.parseLastActive(EncryptedStorage.getEncryptedStorage(InboxWindow.KEY_LAST_ACTIVE))
        val since = InboxWindow.since(now, lastActive)
        Log.d(
            "RobosatsNostrClient",
            "Relay subscription authors: ${authors.size} since=$since limit=${InboxWindow.LIMIT}",
        )
        Client.sendFilter(
            subscriptionNotificationId,
            listOf(
                TypedFilter(
                    types = COMMON_FEED_TYPES,
                    filter = InboxFilter(authors, since, InboxWindow.LIMIT),
                ),
            ),
        )
    }
}

/**
 * 0.9.13: kind 1059 to the robots' pubkeys (`#p`) with a fixed `since` and a `limit`. The
 * library's SincePerRelayFilter takes its since per relay URL from an EOSE map we do not keep,
 * so it always went out without one.
 */
class InboxFilter(
    private val pubKeys: List<String>,
    private val since: Long,
    private val limit: Int,
) : IPerRelayFilter {
    private val pubKeySet = pubKeys.toHashSet()

    override fun toJson(forRelay: String): String =
        FilterSerializer.toJson(null, null, listOf(1059), mapOf("p" to pubKeys), since, null, limit, null)

    override fun match(event: Event, forRelay: String): Boolean =
        event.kind == 1059 && event.tags.any { it.size > 1 && it[0] == "p" && it[1] in pubKeySet }

    override fun toDebugJson(): String = toJson("")

    override fun isValidFor(forRelay: String): Boolean = true
}
