package com.robosats.tor

import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.DataInputStream
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ServerSocket
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * 0.9.13: a request routed "via tor" while Tor is still starting waits for the SOCKS proxy and
 * then goes through it; it never connects directly and never asks the system resolver for the
 * .onion (0.9.12: "Unable to resolve host x.onion" in Clearnet mode right after start-up).
 */
class ProxyWaiterTest {
    private val onion = "roblowuc54m3gjphogvbpcqrbwffedaayji2x243w5ei2h4wwsj2rbid.onion"
    private val dnsLookups = Collections.synchronizedList(ArrayList<String>())
    private val recordingDns = Dns { host ->
        dnsLookups.add(host)
        throw java.net.UnknownHostException("system DNS must not be used: $host")
    }
    private var server: ServerSocket? = null

    @After
    fun tearDown() {
        server?.close()
    }

    /** Minimal SOCKS5 server: records the requested host, then refuses the connection. */
    private fun fakeSocks(requested: AtomicReference<String>, done: CountDownLatch): Proxy {
        val ss = ServerSocket(0, 50, InetAddress.getLoopbackAddress())
        server = ss
        Thread {
            try {
                ss.accept().use { s ->
                    val inp = DataInputStream(s.getInputStream())
                    val out = s.getOutputStream()
                    val ver = inp.readUnsignedByte()
                    if (ver == 5) {
                        val n = inp.readUnsignedByte()
                        repeat(n) { inp.readUnsignedByte() }
                        out.write(byteArrayOf(5, 0)); out.flush()
                        inp.readUnsignedByte(); inp.readUnsignedByte(); inp.readUnsignedByte()
                        val atyp = inp.readUnsignedByte()
                        val host = when (atyp) {
                            3 -> { val len = inp.readUnsignedByte(); ByteArray(len).also { inp.readFully(it) }.toString(Charsets.US_ASCII) }
                            1 -> "IPv4:" + ByteArray(4).also { inp.readFully(it) }.joinToString(".") { (it.toInt() and 0xff).toString() }
                            else -> "atyp$atyp"
                        }
                        requested.set(host)
                        // general failure
                        out.write(byteArrayOf(5, 1, 0, 1, 0, 0, 0, 0, 0, 0)); out.flush()
                    } else {
                        requested.set("socks$ver")
                    }
                }
            } catch (e: IOException) {
                // closed
            } finally {
                done.countDown()
            }
        }.apply { isDaemon = true; start() }
        return Proxy(Proxy.Type.SOCKS, InetSocketAddress(InetAddress.getLoopbackAddress(), ss.localPort))
    }

    @Test
    fun onionRequestWaitsForTorAndNeverResolvesLocally() {
        val requested = AtomicReference<String>()
        val done = CountDownLatch(1)
        val socks = fakeSocks(requested, done)
        val readyAt = System.currentTimeMillis() + 700
        val waiter = ProxyWaiter({ if (System.currentTimeMillis() >= readyAt) socks else null }, pollMs = 50)

        val client = OkHttpClient.Builder()
            .proxySelector(waiter.selector(10_000))
            .dns(recordingDns)
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(5, TimeUnit.SECONDS)
            .build()
        val started = System.currentTimeMillis()
        try {
            client.newCall(Request.Builder().url("http://$onion/api/info/").build()).execute().close()
            fail("the fake SOCKS server refuses every connection")
        } catch (e: IOException) {
            // expected: refused by the (fake) Tor
        }
        assertTrue("waited for Tor", System.currentTimeMillis() - started >= 600)
        assertTrue(done.await(5, TimeUnit.SECONDS))
        assertEquals(onion, requested.get())
        assertEquals(emptyList<String>(), dnsLookups.toList())
    }

    @Test
    fun torThatNeverComesFailsWithoutDnsOrDirectConnection() {
        val waiter = ProxyWaiter({ null }, pollMs = 20)
        assertNull(waiter.await(100))
        val client = OkHttpClient.Builder()
            .proxySelector(waiter.selector(200))
            .dns(recordingDns)
            .connectTimeout(5, TimeUnit.SECONDS)
            .build()
        try {
            client.newCall(Request.Builder().url("http://$onion/api/info/").build()).execute().close()
            fail("must fail")
        } catch (e: IOException) {
            assertTrue(e !is java.net.UnknownHostException)
        }
        assertEquals(emptyList<String>(), dnsLookups.toList())
    }

    @Test
    fun readyProxyIsUsedAtOnce() {
        val p = Proxy(Proxy.Type.SOCKS, InetSocketAddress(InetAddress.getLoopbackAddress(), 9254))
        val waiter = ProxyWaiter({ p })
        val t = System.currentTimeMillis()
        assertEquals(p, waiter.await(5_000))
        assertTrue(System.currentTimeMillis() - t < 100)
        assertEquals(listOf(p), waiter.selector(5_000).select(java.net.URI("http://$onion/")))
    }
}
