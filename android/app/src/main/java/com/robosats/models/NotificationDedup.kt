package com.robosats.models

/**
 * Which coordinator notices (the rumors inside the gift wraps) the Android notification may
 * show. 0.9.6: a notice is shown once, ever: the ids of the rumors already shown are kept on the
 * device (not only in memory), so a relay that sends the same gift wrap again after a restart or
 * a reconnection no longer brings back an old "Order 24 finished".
 *
 * Up to 0.9.3 the rule was "newer than the last one shown" (`<`), which hid a second notice of
 * the same second; 0.9.4 changed it to `<=`, and since the in-memory set of seen gift wraps is
 * lost when the service restarts, the last notice came back on every restart. Now: never a rumor
 * id already shown, and never one older than the last shown (an old notice the relay still
 * keeps); two different notices of the same second both show.
 */
object NotificationDedup {
    const val KEY_LAST = "last_notification"
    const val KEY_SEEN = "notified_rumors"
    const val MAX_SEEN = 300

    interface Store {
        fun get(key: String): String
        fun set(key: String, value: String)
    }

    /** Parses "id:createdAt;id:createdAt…" (no JSON: org.json is a stub in local unit tests). */
    fun parse(raw: String): MutableMap<String, Long> {
        val out = LinkedHashMap<String, Long>()
        if (raw.isEmpty()) return out
        for (item in raw.split(';')) {
            val i = item.lastIndexOf(':')
            if (i <= 0) continue
            val ts = item.substring(i + 1).toLongOrNull() ?: continue
            out[item.substring(0, i)] = ts
        }
        return out
    }

    fun format(seen: Map<String, Long>): String =
        seen.entries
            .sortedByDescending { it.value }
            .take(MAX_SEEN)
            .joinToString(";") { "${it.key}:${it.value}" }

    /**
     * True when the notice should be shown now; it is then recorded as shown (its id, and the
     * last-shown time if it is newer). False for a notice already shown or older than the last.
     */
    @Synchronized
    fun shouldNotify(store: Store, rumorId: String, createdAt: Long): Boolean {
        val seen = parse(store.get(KEY_SEEN))
        if (seen.containsKey(rumorId)) return false
        val last = store.get(KEY_LAST).toLongOrNull()
        if (last != null && createdAt < last) return false
        seen[rumorId] = createdAt
        store.set(KEY_SEEN, format(seen))
        if (last == null || createdAt > last) store.set(KEY_LAST, createdAt.toString())
        return true
    }
}
