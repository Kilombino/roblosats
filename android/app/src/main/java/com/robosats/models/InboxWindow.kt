package com.robosats.models

/**
 * 0.9.13: how far back the notification service asks the relays for gift wraps (kind 1059), and
 * which gift wraps it has already opened. Up to 0.9.12 the filter had no `since` and no `limit`:
 * every start (and every resubscription, one per garage write) brought back the whole history
 * of every robot, hundreds of events and ~170 MB of garbage at once on a phone with 10 robots.
 *
 * NIP-59 gift wraps carry a created_at randomised up to 2 days into the past, so the window
 * always reaches [JITTER_SECONDS] before the last time the service was listening, and never more
 * than [MAX_BACK_SECONDS] before now (a phone off for weeks does not need old notices: the order
 * screens show the state). Notices themselves are still deduplicated by rumor id
 * (NotificationDedup); [SeenWraps] only saves unwrapping the same gift wrap again.
 */
object InboxWindow {
    const val KEY_LAST_ACTIVE = "notifications_last_active"
    const val JITTER_SECONDS = 2L * 24 * 3600 + 3600
    const val FIRST_RUN_BACK_SECONDS = 3L * 24 * 3600
    const val MAX_BACK_SECONDS = 7L * 24 * 3600
    const val LIMIT = 300

    /** The `since` of the inbox filter, in seconds. */
    fun since(nowSeconds: Long, lastActiveSeconds: Long?): Long {
        val floor = nowSeconds - MAX_BACK_SECONDS
        val wanted = if (lastActiveSeconds == null || lastActiveSeconds <= 0 || lastActiveSeconds > nowSeconds + 600) {
            nowSeconds - FIRST_RUN_BACK_SECONDS
        } else {
            lastActiveSeconds - JITTER_SECONDS
        }
        return maxOf(floor, wanted)
    }

    fun parseLastActive(raw: String): Long? = raw.trim().toLongOrNull()
}

/**
 * Gift wrap ids already opened, kept on the device ("id:createdAt;…", newest [max] kept), so a
 * restart does not unwrap again what it already saw inside the window. In memory while the
 * service runs; [dirty] says it must be written back (the service does it once a minute, not
 * once per event).
 */
class SeenWraps(raw: String, private val max: Int = MAX) {
    private val seen: MutableMap<String, Long> = NotificationDedup.parse(raw)

    @Volatile
    var dirty = false
        private set

    /** True the first time [id] is seen (and records it). */
    @Synchronized
    fun firstTime(id: String, createdAt: Long): Boolean {
        if (seen.containsKey(id)) return false
        seen[id] = createdAt
        dirty = true
        return true
    }

    @Synchronized
    fun size(): Int = seen.size

    @Synchronized
    fun serialize(): String {
        dirty = false
        val kept = seen.entries.sortedByDescending { it.value }.take(max)
        if (kept.size < seen.size) {
            val keep = kept.map { it.key }.toHashSet()
            seen.keys.retainAll(keep)
        }
        return kept.joinToString(";") { "${it.key}:${it.value}" }
    }

    companion object {
        const val KEY = "notification_seen_wraps"
        const val MAX = 2000
    }
}
