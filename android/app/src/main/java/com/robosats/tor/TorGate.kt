package com.robosats.tor

import android.app.Application
import android.util.Log
import com.robosats.models.EncryptedStorage
import com.vitorpamplona.ammolite.service.HttpClientManager
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ProxySelector
import java.net.SocketAddress
import java.net.URI

/**
 * 0.9.13: everything that must go through Tor waits for Tor's SOCKS proxy and is never sent
 * without it.
 *
 * Up to 0.9.12 the requests routed "via tor" were built with `.proxy(torKmp.proxy)`; while the
 * built-in Tor was still starting that proxy was null, OkHttp took it as "no proxy" and went
 * direct, asking the system resolver for the .onion ("Unable to resolve host x.onion"). Seen in
 * Clearnet mode (the page opens before Tor) and in the notification service's relays (the
 * shared HttpClientManager has no proxy until Tor reached 100 %).
 *
 * Now: OkHttp gets a [ProxySelector] that blocks the call's own (dispatcher) thread until the
 * proxy exists, up to [WAIT_MS], and then returns it; if Tor never comes, it returns
 * [UNREACHABLE], a SOCKS proxy on a closed loopback port: the call fails at once and no name is
 * ever resolved locally (with a SOCKS proxy OkHttp hands the host name to the proxy).
 */
object TorGate {
    private const val TAG = "TorGate"
    const val WAIT_MS = 120_000L

    /** A SOCKS proxy nobody listens on: fails fast, never resolves names locally. */
    val UNREACHABLE: Proxy = Proxy(Proxy.Type.SOCKS, InetSocketAddress(InetAddress.getLoopbackAddress(), 1))

    /** The built-in Tor's SOCKS proxy when Tor is connected; null otherwise. */
    fun readyProxy(): Proxy? = try {
        val tor = TorKmpManager.getTorKmpObject()
        val p = tor.proxy
        if (p != null && tor.isConnected()) p else null
    } catch (e: UninitializedPropertyAccessException) {
        null
    } catch (e: Exception) {
        null
    }

    val waiter = ProxyWaiter({ readyProxy() })

    /** For OkHttp builders: `builder.proxySelector(TorGate.selector)` (and no `.proxy(...)`). */
    val selector: ProxySelector = waiter.selector(WAIT_MS)

    /** Blocking: Tor's proxy, or [UNREACHABLE] after [timeoutMs]. Never call it on the main or JS bridge thread. */
    fun awaitProxy(timeoutMs: Long = WAIT_MS): Proxy = waiter.await(timeoutMs) ?: UNREACHABLE

    private fun useBuiltInTor(): Boolean = try {
        EncryptedStorage.getEncryptedStorage("settings_use_proxy") != "false"
    } catch (e: Exception) {
        true
    }

    @Volatile
    private var relayWaiter: Thread? = null

    /** False while the built-in Tor is in use but not ready: relays must not be dialled yet. */
    fun relaysCanConnect(): Boolean = !useBuiltInTor() || readyProxy() != null

    /**
     * Notification relays (ammolite's shared HttpClientManager, which only takes a fixed Proxy):
     * Orbot mode → no proxy (Orbot's VPN carries it); built-in Tor → Tor's proxy. Returns true
     * when the relays can be connected now; otherwise the shared client points at
     * [UNREACHABLE] (never "no proxy"), the built-in Tor is started if nobody did (service
     * without the app) and [onReady] runs once Tor is ready. Not dialling before that matters:
     * a relay that failed waits 180 s before it tries again (ammolite's Relay).
     */
    fun prepareRelays(application: Application, onReady: () -> Unit): Boolean {
        if (!useBuiltInTor()) {
            HttpClientManager.setDefaultProxy(null)
            return true
        }
        val ready = readyProxy()
        HttpClientManager.setDefaultProxy(ready ?: UNREACHABLE)
        if (ready != null) return true

        val tor = try {
            TorKmpManager.getTorKmpObject()
        } catch (e: UninitializedPropertyAccessException) {
            TorKmp(application).also { TorKmpManager.updateTorKmpObject(it) }
        }
        if (!tor.isConnected() && !tor.isStarting()) tor.startQuietly()

        synchronized(this) {
            if (relayWaiter?.isAlive == true) return false
            relayWaiter = Thread {
                val p = waiter.await(WAIT_MS)
                if (p != null) {
                    Log.i(TAG, "Tor ready: relays now use $p")
                    HttpClientManager.setDefaultProxy(p)
                    try {
                        onReady()
                    } catch (t: Throwable) {
                        Log.w(TAG, "relays onReady: ${t.message}")
                    }
                } else {
                    Log.w(TAG, "Tor not ready after ${WAIT_MS / 1000} s: relays stay off (no direct connection)")
                }
            }.apply {
                name = "TorGate-relays"
                isDaemon = true
                start()
            }
        }
        return false
    }

    /** Called when Tor's listeners change: relays follow the new proxy (never "no proxy"). */
    fun onTorProxyChanged(proxy: Proxy?) {
        if (!useBuiltInTor()) return
        HttpClientManager.setDefaultProxy(proxy ?: UNREACHABLE)
    }
}

/**
 * Waits for a proxy from [provider] (polled every [pollMs]); pure JVM, unit-tested with a fake
 * provider and a fake SOCKS server.
 */
class ProxyWaiter(
    private val provider: () -> Proxy?,
    private val pollMs: Long = 200,
) {
    fun await(timeoutMs: Long): Proxy? {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000
        while (true) {
            provider()?.let { return it }
            if (System.nanoTime() >= deadline) return null
            try {
                Thread.sleep(pollMs)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                return null
            }
        }
    }

    fun selector(timeoutMs: Long, unreachable: Proxy = TorGate_UNREACHABLE): ProxySelector =
        object : ProxySelector() {
            override fun select(uri: URI?): List<Proxy> = listOf(await(timeoutMs) ?: unreachable)

            override fun connectFailed(uri: URI?, sa: SocketAddress?, ioe: IOException?) {}
        }
}

/** Same as TorGate.UNREACHABLE, without touching TorGate (Android classes) from plain JVM tests. */
val TorGate_UNREACHABLE: Proxy = Proxy(Proxy.Type.SOCKS, InetSocketAddress(InetAddress.getLoopbackAddress(), 1))
