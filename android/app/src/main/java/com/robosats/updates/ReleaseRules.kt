package com.robosats.updates

/**
 * 0.9.6: the pure rules of the in-app update (as in Kilowallet): which release is newer, which
 * APK fits this phone and which SHA-256 the release notes publish for it. No Android here, so
 * they are unit-tested on the JVM.
 */
object ReleaseRules {
    /** "0.9.6-beta2" → [0, 9, 6] plus whether it is a pre-release. */
    private fun parse(v: String): Pair<List<Int>, Boolean> {
        val clean = v.removePrefix("v").trim()
        val nums = clean.substringBefore('-').split('.').map { it.toIntOrNull() ?: 0 }
        return nums to clean.contains('-')
    }

    /** True when [remote] is newer than [current] (a final release beats its own betas). */
    fun isNewer(remote: String, current: String): Boolean {
        val (r, rPre) = parse(remote)
        val (c, cPre) = parse(current)
        for (i in 0 until maxOf(r.size, c.size)) {
            val a = r.getOrElse(i) { 0 }
            val b = c.getOrElse(i) { 0 }
            if (a != b) return a > b
        }
        return cPre && !rPre
    }

    private val ABI_ORDER = listOf("arm64-v8a", "armeabi-v7a", "x86_64")

    /**
     * The APK of the release for this phone: the split for its first supported ABI
     * ("roblosats-v0.9.6-arm64-v8a.apk"), else the universal one. Null when none fits.
     */
    fun pickApk(names: List<String>, supportedAbis: List<String>): String? {
        val apks = names.filter { it.startsWith("roblosats-v") && it.endsWith(".apk") }
        for (abi in supportedAbis) {
            if (abi !in ABI_ORDER) continue
            apks.firstOrNull { it.endsWith("-$abi.apk") }?.let { return it }
        }
        return apks.firstOrNull { it.endsWith("-universal.apk") }
    }

    private val SUM_LINE = Regex("(?m)^\\s*([0-9a-f]{64})\\s+\\*?(\\S+\\.apk)\\s*$")
    private val SIGNED_LINE = Regex("Signed APK SHA-256:\\s*`([0-9a-f]{64})`")

    /**
     * The SHA-256 the release notes publish for [apkName]: its line of the "### SHA-256" block
     * ("<hash>  <file>"), or, for a release with a single APK, the "Signed APK SHA-256: `…`"
     * line. Null when the notes do not publish it: then the app only offers the page.
     */
    fun hashFor(body: String, apkName: String): String? {
        SUM_LINE.findAll(body).forEach { m -> if (m.groupValues[2] == apkName) return m.groupValues[1] }
        val signed = SIGNED_LINE.findAll(body).map { it.groupValues[1] }.toList()
        return if (signed.size == 1) signed[0] else null
    }
}
