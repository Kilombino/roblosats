package com.robosats.updates

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.net.Proxy
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * 0.9.6: the in-app update, as in Kilowallet. [check] looks at the latest release of
 * Kilombino/roblosats on GitHub (through Tor when the app uses its own Tor); [start] downloads
 * the APK for this phone, checks it and hands it to Android's installer.
 *
 * Nothing is installed unless the download's SHA-256 is the one the release notes publish for
 * that file AND it is signed with the same certificate as the app already installed. A release
 * whose notes do not publish the hash is only offered as a page to open.
 */
object AppUpdater {
    private const val TAG = "RoblosatsUpdate"
    private const val API = "https://api.github.com/repos/Kilombino/roblosats/releases/latest"

    class Release(
        val version: String,
        val url: String,
        val notes: String,
        val apkName: String?,
        val apkUrl: String?,
        val sha256: String?,
    ) {
        val canInstall: Boolean get() = apkUrl != null && sha256 != null
    }

    @Volatile var last: Release? = null
        private set

    /** Called with a JSON state ({state, percent?, message?}) for the page. */
    @Volatile var onState: ((String) -> Unit)? = null

    private fun client(proxy: Proxy?): OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .apply { if (proxy != null) proxy(proxy) }
            .build()

    private fun state(name: String, percent: Int? = null, message: String? = null) {
        val o = JSONObject().put("state", name)
        if (percent != null) o.put("percent", percent)
        if (message != null) o.put("message", message)
        onState?.invoke(o.toString())
    }

    private const val PAGES = "https://github.com/Kilombino/roblosats/releases"

    private fun getText(proxy: Proxy?, url: String): String? {
        val req = Request.Builder().url(url).header("User-Agent", "Roblosats").build()
        return client(proxy).newCall(req).execute().use { r -> if (r.isSuccessful) r.body.string() else null }
    }

    /**
     * Without the API: the tag from where /releases/latest redirects, the hashes from the
     * release's SHA256SUMS and, when the release has one, its notes from RELEASE_NOTES.md.
     */
    private fun checkWithoutApi(ctx: Context, proxy: Proxy?, current: String): String {
        val noRedirect = client(proxy).newBuilder().followRedirects(false).followSslRedirects(false).build()
        val location = noRedirect.newCall(
            Request.Builder().url("$PAGES/latest").header("User-Agent", "Roblosats").build(),
        ).execute().use { it.header("Location") } ?: error("no redirect from releases/latest")
        val tag = location.substringAfterLast("/tag/", "").ifEmpty { error("unexpected redirect $location") }
        if (!ReleaseRules.isNewer(tag, current)) {
            return JSONObject().put("status", "uptodate").put("current", current).toString()
        }
        val version = tag.removePrefix("v")
        val sums = getText(proxy, "$PAGES/download/$tag/SHA256SUMS") ?: ""
        val notes = getText(proxy, "$PAGES/download/$tag/RELEASE_NOTES.md") ?: ""
        val names = Regex("(?m)^\\s*[0-9a-f]{64}\\s+\\*?(\\S+)\\s*$").findAll(sums).map { it.groupValues[1] }.toList()
        val apk = ReleaseRules.pickApk(names, Build.SUPPORTED_ABIS.toList())
        val sha = apk?.let { ReleaseRules.hashFor(sums, it) }
        val r = Release(version, "$PAGES/tag/$tag", notes, apk, apk?.let { "$PAGES/download/$tag/$it" }, sha)
        last = r
        return JSONObject().put("status", "newer").put("version", r.version).put("current", current)
            .put("url", r.url).put("notes", r.notes).put("canInstall", r.canInstall).toString()
    }

    /** {status: newer|uptodate|unreachable, version, url, notes, canInstall}. Blocking. */
    fun check(ctx: Context, proxy: Proxy?): String {
        val current = runCatching { ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName }.getOrNull() ?: "0"
        return runCatching {
            val req = Request.Builder().url(API)
                .header("Accept", "application/vnd.github+json")
                .header("User-Agent", "Roblosats")
                .build()
            val body = client(proxy).newCall(req).execute().use { r ->
                if (!r.isSuccessful) {
                    // GitHub's API often answers 403 to Tor exits (shared rate limit): use the
                    // plain release pages, which it does serve
                    Log.w(TAG, "API answered HTTP ${r.code}: trying the release pages")
                    return checkWithoutApi(ctx, proxy, current)
                }
                r.body.string()
            }
            val o = JSONObject(body)
            val tag = o.getString("tag_name")
            if (!ReleaseRules.isNewer(tag, current)) {
                return JSONObject().put("status", "uptodate").put("current", current).toString()
            }
            val assets = o.optJSONArray("assets")
            val byName = (0 until (assets?.length() ?: 0)).associate {
                val a = assets!!.getJSONObject(it)
                a.optString("name") to a.optString("browser_download_url")
            }
            val notes = o.optString("body")
            val apk = ReleaseRules.pickApk(byName.keys.toList(), Build.SUPPORTED_ABIS.toList())
            val sha = apk?.let { ReleaseRules.hashFor(notes, it) }
            val r = Release(tag.removePrefix("v"), o.getString("html_url"), notes, apk, apk?.let { byName[it] }, sha)
            last = r
            JSONObject().put("status", "newer").put("version", r.version).put("current", current)
                .put("url", r.url).put("notes", r.notes).put("canInstall", r.canInstall).toString()
        }.getOrElse {
            Log.w(TAG, "update check failed: ${it.message}")
            JSONObject().put("status", "unreachable").put("current", current).toString()
        }
    }

    /** Download, verify and install [last]. Progress goes to [onState]. Blocking. */
    fun start(ctx: Context, proxy: Proxy?) {
        val app = ctx.applicationContext
        val r = last ?: return state("failed", message = "Check for updates first.")
        runCatching {
            val url = r.apkUrl ?: error("This release has no APK for this phone.")
            val f = download(app, url, r, proxy)
            state("verifying")
            verify(app, f, r)
            install(app, f)
        }.onFailure { state("failed", message = it.message ?: "The update failed.") }
    }

    private fun download(ctx: Context, url: String, r: Release, proxy: Proxy?): File {
        val dir = File(ctx.cacheDir, "updates").apply { mkdirs(); listFiles()?.forEach { it.delete() } }
        val out = File(dir, r.apkName ?: "roblosats.apk")
        state("downloading", 0)
        val req = Request.Builder().url(url).header("User-Agent", "Roblosats").build()
        client(proxy).newCall(req).execute().use { resp ->
            check(resp.isSuccessful) { "Download failed (HTTP ${resp.code})." }
            val total = resp.body.contentLength()
            var done = 0L
            var lastPct = -1
            resp.body.byteStream().use { input ->
                out.outputStream().use { o ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        o.write(buf, 0, n)
                        done += n
                        val pct = if (total > 0) (done * 100 / total).toInt() else 0
                        if (pct != lastPct) { lastPct = pct; state("downloading", pct) }
                    }
                }
            }
        }
        return out
    }

    private fun sha256(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().use { i ->
            val b = ByteArray(64 * 1024)
            while (true) { val n = i.read(b); if (n < 0) break; md.update(b, 0, n) }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    @Suppress("DEPRECATION")
    private fun certs(info: android.content.pm.PackageInfo?): Set<String> {
        if (info == null) return emptySet()
        val sigs = if (Build.VERSION.SDK_INT >= 28) info.signingInfo?.apkContentsSigners else info.signatures
        return sigs.orEmpty().map { s ->
            MessageDigest.getInstance("SHA-256").digest(s.toByteArray()).joinToString("") { "%02x".format(it) }
        }.toSet()
    }

    @Suppress("DEPRECATION")
    private fun verify(ctx: Context, f: File, r: Release) {
        val expected = r.sha256 ?: run { f.delete(); error("This release does not publish its APK's SHA-256. Nothing was installed: update from its page.") }
        check(sha256(f) == expected) { f.delete(); "The downloaded file is not the published one (SHA-256 differs). Nothing was installed." }
        val pm = ctx.packageManager
        val flag = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
        val theirs = certs(pm.getPackageArchiveInfo(f.absolutePath, flag))
        val ours = certs(pm.getPackageInfo(ctx.packageName, flag))
        check(theirs.isNotEmpty() && theirs == ours) { f.delete(); "The download is not signed with this app's certificate. Nothing was installed." }
        val pkg = pm.getPackageArchiveInfo(f.absolutePath, 0)?.packageName
        check(pkg == ctx.packageName) { f.delete(); "The download is another app. Nothing was installed." }
    }

    private var pending: File? = null

    private fun install(ctx: Context, f: File) {
        pending = f
        val pm = ctx.packageManager
        if (Build.VERSION.SDK_INT >= 26 && !pm.canRequestPackageInstalls()) {
            state("needs-permission")
            return
        }
        state("installing")
        val installer = pm.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(ctx.packageName)
            if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            session.openWrite("roblosats.apk", 0, f.length()).use { out ->
                f.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            val intent = Intent(ctx, InstallResultReceiver::class.java)
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
            session.commit(PendingIntent.getBroadcast(ctx, 7, intent, flags).intentSender)
        }
    }

    /** After the user allowed installs from this app in Android's settings. */
    fun resume(ctx: Context) {
        val f = pending ?: return state("failed", message = "Nothing downloaded to install.")
        runCatching { install(ctx.applicationContext, f) }
            .onFailure { state("failed", message = it.message ?: "The update failed.") }
    }

    class InstallResultReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
                PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                    @Suppress("DEPRECATION")
                    val confirm = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                    else intent.getParcelableExtra(Intent.EXTRA_INTENT)
                    confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)?.let { runCatching { context.startActivity(it) } }
                }
                PackageInstaller.STATUS_SUCCESS -> state("installed")
                else -> state(
                    "failed",
                    message = "Android did not install it: " +
                        (intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "cancelled or failed") + ".",
                )
            }
        }
    }
}
