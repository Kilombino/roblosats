package com.robosats

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.widget.Toast
import com.robosats.models.EncryptedStorage
import com.robosats.models.NostrClient
import com.robosats.services.NotificationsService
import com.robosats.tor.TorGate
import com.robosats.tor.TorKmpManager.getTorKmpObject
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient.Builder
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.io.IOException
import java.net.MalformedURLException
import java.net.URL
import java.util.concurrent.TimeUnit
import java.util.regex.Pattern
import okhttp3.Request.Builder as RequestBuilder


/**
 * Provides a secure bridge between JavaScript and native Android code.
 * This class is designed with security in mind, implementing input validation,
 * sanitization, and proper error handling.
 */
@SuppressLint("SetJavaScriptEnabled")
class WebAppInterface(private val context: MainActivity, private val webView: WebView) {
    private val TAG = "WebAppInterface"
    private val roboIdentities = RoboIdentities()
    private val webSockets: MutableMap<String?, WebSocket?> = HashMap<String?, WebSocket?>()

    // Security patterns for input validation
    private val UUID_PATTERN = Pattern.compile("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", Pattern.CASE_INSENSITIVE)
    private val SAFE_STRING_PATTERN = Pattern.compile("^[a-zA-Z0-9\\s_\\-.,:;!?()\\[\\]{}\"]*$")

    init {
        // Check if libraries are loaded and show a toast notification if there's an issue
        if (!RoboIdentities.areLibrariesLoaded()) {
            Log.w(TAG, "Native libraries for RoboIdentities are not loaded - fallback names will be used")
            Toast.makeText(
                context,
                "Warning: Robot name generator is using fallback mode",
                Toast.LENGTH_LONG
            ).show()
        }
    }

    @JavascriptInterface
    fun generateRoboname(uuid: String, message: String) {
        // Validate inputs before processing
        if (!isValidUuid(uuid) || !isValidInput(message)) {
            Log.e(TAG, "Invalid input for generateRoboname: uuid=$uuid, message=$message")
            rejectPromise(uuid, "Invalid input parameters")
            return
        }

        try {
            // Sanitize the input before passing to native code
            val sanitizedMessage = message.trim()

            // Generate the roboname
            val roboname = roboIdentities.generateRoboname(sanitizedMessage)

            // Safely encode and return the result
            resolvePromise(uuid, roboname ?: "")
        } catch (e: Exception) {
            Log.e(TAG, "Error in generateRoboname", e)
            rejectPromise(uuid, "Error generating robot name")
        }
    }

    @JavascriptInterface
    fun generateRobohash(uuid: String, message: String) {
        // Validate inputs before processing
        if (!isValidUuid(uuid) || !isValidInput(message)) {
            Log.e(TAG, "Invalid input for generateRobohash: uuid=$uuid, message=$message")
            rejectPromise(uuid, "Invalid input parameters")
            return
        }

        try {
            // Sanitize the input before passing to native code
            val sanitizedMessage = message.trim()

            // Generate the robohash
            val robohash = roboIdentities.generateRobohash(sanitizedMessage)

            // Safely encode and return the result
            resolvePromise(uuid, robohash ?: "")
        } catch (e: Exception) {
            Log.e(TAG, "Error in generateRobohash", e)
            rejectPromise(uuid, "Error generating robot hash")
        }
    }

    @JavascriptInterface
    fun copyToClipboard(message: String) {
        try {
            // Copy to clipboard
            val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as android.content.ClipboardManager
            val clip = android.content.ClipData.newPlainText("RoboSats Data", message)
            clipboard.setPrimaryClip(clip)

            // Show a toast notification
            Toast.makeText(context, "Copied to clipboard", Toast.LENGTH_SHORT).show()

            // Log the action (don't log the content for privacy)
            Log.d(TAG, "Text copied to clipboard")
        } catch (e: Exception) {
            Log.e(TAG, "Error copying to clipboard", e)
            Toast.makeText(context, "Failed to copy to clipboard", Toast.LENGTH_SHORT).show()
        }
    }

    @JavascriptInterface
    fun getTorStatus(uuid: String) {
        // Validate UUID
        if (!isValidUuid(uuid)) {
            Log.e(TAG, "Invalid UUID for getTorStatus: $uuid")
            return
        }

        try {
            // Orbot mode never starts the built-in Tor: there is no Tor object to ask
            // (it threw "lateinit property torKmp has not been initialized" every 5 s).
            if (!context.useProxy) {
                resolvePromise(uuid, "OFF")
                return
            }
            // Get Tor status safely
            val torState = getTorKmpObject().torState.state.name

            // Return the status through the secure promise resolution
            resolvePromise(uuid, torState)
        } catch (e: Exception) {
            Log.e(TAG, "Error getting Tor status", e)
            rejectPromise(uuid, "Error retrieving Tor status")
        }
    }

    @JavascriptInterface
    fun openWS(uuid: String, path: String) {
        // Validate UUID
        if (!isValidUuid(uuid)) {
            Log.e(TAG, "Invalid UUID for openWS: $uuid")
            return
        }
        if (!isValidUrl(path)) {
            Log.e(TAG, "Invalid or disallowed URL for openWS: $path")
            rejectPromise(uuid, "Invalid URL")
            return
        }

        try {
            Log.d(TAG, "WebSocket opening: $path")
            // Create OkHttpClient
            // readTimeout must be 0 (disabled) for long-lived WebSocket connections;
            // a non-zero value causes OkHttp to kill the socket after the timeout
            // fires during a quiet period.  A ping interval keeps the connection alive
            // through NAT/firewall idle timeouts instead.
            var builder = Builder()
                .connectTimeout(60, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.SECONDS)
                .pingInterval(30, TimeUnit.SECONDS)

            if (useTorFor(path)) {
                builder = builder.proxySelector(TorGate.selector)
            }

            val client = builder.build()

            // Create a request for the WebSocket connection
            val request: Request = RequestBuilder()
                .url(path)
                .build()


            // Create a WebSocket listener
            val listener: WebSocketListener = object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    Log.d(TAG, "WebSocket opened: " + response.message)
                    synchronized(webSockets) {
                        webSockets.put(
                            path,
                            webSocket
                        ) // Store the WebSocket instance with its URL
                    }
                    resolvePromise(uuid, path)
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    onWsMessage(path, text)
                }

                override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                    onWsMessage(path, bytes.hex())
                }

                // 0.9.10 (#29): the map is keyed by URL and the app reconnects to the same URL,
                // so only the socket that is still the current one for its URL may remove itself
                // and tell JS; an old socket closing late used to drop the live one. A failure
                // after the socket was open (a Tor circuit dropping) now tells JS too: before,
                // JS believed it was still open and the chat never received anything again.
                private fun dropIfCurrent(webSocket: WebSocket): Boolean {
                    synchronized(webSockets) {
                        if (webSockets[path] === webSocket) {
                            webSockets.remove(path)
                            return true
                        }
                        return false
                    }
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    Log.d(TAG, "WebSocket closing: " + reason)
                    webSocket.close(1000, null)
                    if (dropIfCurrent(webSocket)) onWsClose(path)
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    if (dropIfCurrent(webSocket)) onWsClose(path)
                }

                override fun onFailure(
                    webSocket: WebSocket,
                    t: Throwable,
                    response: Response?
                ) {
                    Log.d(TAG, "WebSocket error: " + t.message)
                    if (dropIfCurrent(webSocket)) {
                        onWsError(path)
                    } else {
                        rejectPromise(uuid, "false")
                    }
                }
            }

            client.newWebSocket(request, listener)
        } catch (e: Exception) {
            Log.e(TAG, "Error connecting to WebSocket", e)
            rejectPromise(uuid, "Error connecting Tor WebSocket")
        }
    }

    @JavascriptInterface
    fun sendWsMessage(uuid: String, path: String, message: String) {
        // Validate UUID
        if (!isValidUuid(uuid)) {
            Log.e(TAG, "Invalid UUID for getTorStatus: $uuid")
            return
        }

        val websocket = webSockets[path]
        if (websocket != null) {
            websocket.send(message)
            resolvePromise(uuid, "true")
        } else {
            rejectPromise(uuid, "Error sending WebSocket message")
        }
    }

    @JavascriptInterface
    fun sendBinary(uuid: String, url: String, headers: String, base64Data: String) {
        // Validate inputs
        if (!isValidUuid(uuid)) {
            Log.e(TAG, "Invalid UUID for sendBinary: $uuid")
            rejectPromise(uuid, "Invalid UUID")
            return
        }
        if (!isValidUrl(url)) {
            Log.e(TAG, "Invalid or disallowed URL for sendBinary: $url")
            rejectPromise(uuid, "Invalid URL")
            return
        }

        try {
            // Decode base64 to byte array
            val binaryData = android.util.Base64.decode(base64Data, android.util.Base64.DEFAULT)

            // Create OkHttpClient
            var builder = Builder()
                .connectTimeout(60, TimeUnit.SECONDS) // Set connection timeout
                .readTimeout(120, TimeUnit.SECONDS) // Set read timeout

            if (useTorFor(url)) {
                builder = builder.proxySelector(TorGate.selector)
            }

            val client = builder.build()

            // Build request with URL
            val requestBuilder = RequestBuilder().url(url)

            // Add headers from JSON and extract Content-Type
            val headersObject = JSONObject(headers)
            var contentType = "application/octet-stream" // Default content type
            val keys = headersObject.keys()
            while (keys.hasNext()) {
                val key = keys.next()
                val value = headersObject.optString(key)
                requestBuilder.addHeader(key, value)
                if (key.equals("Content-Type", ignoreCase = true)) {
                    contentType = value
                }
            }

            // Create request body with binary data
            val mediaType = contentType.toMediaType()
            val requestBody = binaryData.toRequestBody(mediaType)
            requestBuilder.put(requestBody)

            // Build and execute request
            val request = requestBuilder.build()
            client.newCall(request).enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    Log.d("RobosatsError", e.toString())
                    rejectPromise(uuid, "Binary upload failed: ${e.message}")
                }

                override fun onResponse(call: Call, response: Response) {
                    try {
                        // Get response body
                        val responseBody = response.body.string()

                        // Create JSON object with headers
                        val headersJson = JSONObject()
                        response.headers.names().forEach { name ->
                            headersJson.put(name, response.header(name))
                        }

                        resolvePromise(uuid, responseBody)
                    } catch (e: Exception) {
                        Log.e(TAG, "Error processing response", e)
                        rejectPromise(uuid, "Error processing response: ${e.message}")
                    }
                }
            })
        } catch (e: Exception) {
            Log.e(TAG, "Error in sendBinary", e)
            rejectPromise(uuid, "Error sending binary data: ${e.message}")
        }
    }

    /**
     * Plain-text HTTP request for the onchain rescue screen (an explorer, no coordinator):
     * resolves {"status": code, "body": base64}. Through Tor when the app uses it.
     */
    @JavascriptInterface
    fun sendText(uuid: String, method: String, url: String, contentType: String, body: String) {
        if (!isValidUuid(uuid)) {
            rejectPromise(uuid, "Invalid UUID")
            return
        }
        // The user picks this explorer, and it may well be their own node at home (a LAN
        // address): only http(s) is checked here, not the private-address rule of isValidUrl.
        val scheme = url.trim().substringBefore("://").lowercase()
        if ((scheme != "http" && scheme != "https") || (method != "GET" && method != "POST")) {
            rejectPromise(uuid, "Invalid request")
            return
        }
        try {
            var builder = Builder()
                .connectTimeout(60, TimeUnit.SECONDS)
                .readTimeout(120, TimeUnit.SECONDS)
            if (useTorFor(url)) {
                builder = builder.proxySelector(TorGate.selector)
            }
            val client = builder.build()
            val requestBuilder = RequestBuilder().url(url)
            if (method == "POST") {
                val type = (if (contentType.isBlank()) "text/plain" else contentType).toMediaType()
                requestBuilder.post(body.toRequestBody(type))
            }
            client.newCall(requestBuilder.build()).enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    rejectPromise(uuid, "Request failed: ${e.message}")
                }

                override fun onResponse(call: Call, response: Response) {
                    try {
                        val bytes = response.body.bytes()
                        val b64 = android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP)
                        resolvePromise(uuid, "{\"status\":${response.code},\"body\":\"$b64\"}")
                    } catch (e: Exception) {
                        rejectPromise(uuid, "Error reading response: ${e.message}")
                    }
                }
            })
        } catch (e: Exception) {
            rejectPromise(uuid, "Error sending request: ${e.message}")
        }
    }

    @JavascriptInterface
    fun getBinary(uuid: String, url: String) {
        // Validate inputs
        if (!isValidUuid(uuid)) {
            Log.e(TAG, "Invalid UUID for getBinary: $uuid")
            rejectPromise(uuid, "Invalid UUID")
            return
        }
        if (!isValidUrl(url)) {
            Log.e(TAG, "Invalid or disallowed URL for getBinary: $url")
            rejectPromise(uuid, "Invalid URL")
            return
        }

        try {
            // Create OkHttpClient
            var builder = Builder()
                .connectTimeout(60, TimeUnit.SECONDS) // Set connection timeout
                .readTimeout(120, TimeUnit.SECONDS) // Set read timeout

            if (useTorFor(url)) {
                builder = builder.proxySelector(TorGate.selector)
            }

            val client = builder.build()

            // Build request with URL
            val request = RequestBuilder().url(url).build()

            // Execute request
            client.newCall(request).enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    Log.d("RobosatsError", e.toString())
                    rejectPromise(uuid, "Binary download failed: ${e.message}")
                }

                override fun onResponse(call: Call, response: Response) {
                    try {
                        // Get response body as bytes
                        val binaryData = response.body.bytes()

                        // Encode to base64 for JavaScript bridge
                        val base64Data = android.util.Base64.encodeToString(binaryData, android.util.Base64.NO_WRAP)

                        resolvePromise(uuid, base64Data)
                    } catch (e: Exception) {
                        Log.e(TAG, "Error processing binary response", e)
                        rejectPromise(uuid, "Error processing binary response: ${e.message}")
                    }
                }
            })
        } catch (e: Exception) {
            Log.e(TAG, "Error in getBinary", e)
            rejectPromise(uuid, "Error downloading binary data: ${e.message}")
        }
    }

    @JavascriptInterface
    fun sendRequest(uuid: String, action: String, url: String, headers: String, body: String) {
        // Validate inputs
        if (!isValidUuid(uuid)) {
            Log.e(TAG, "Invalid UUID for sendRequest: $uuid")
            rejectPromise(uuid, "Invalid UUID")
            return
        }
        if (!isValidUrl(url)) {
            Log.e(TAG, "Invalid or disallowed URL for sendRequest: $url")
            rejectPromise(uuid, "Invalid URL")
            return
        }

        try {
            // Create OkHttpClient
            var builder = Builder()
                .connectTimeout(60, TimeUnit.SECONDS) // Set connection timeout
                .readTimeout(120, TimeUnit.SECONDS) // Set read timeout

            if (useTorFor(url)) {
                builder = builder.proxySelector(TorGate.selector)
            }

            val client = builder.build()

            // Build request with URL
            val requestBuilder = RequestBuilder().url(url)

            // Add headers from JSON
            val headersObject = JSONObject(headers)
            val keys = headersObject.keys()
            while (keys.hasNext()) {
                val key = keys.next()
                val value = headersObject.optString(key)
                requestBuilder.addHeader(key, value)
            }

            // Set request method and body
            when (action) {
                "DELETE" -> requestBuilder.delete()
                "POST" -> {
                    val mediaType = "application/json; charset=utf-8".toMediaType()
                    val requestBody = body.toRequestBody(mediaType)
                    requestBuilder.post(requestBody)
                }
                "PUT" -> {
                    val mediaType = "application/json; charset=utf-8".toMediaType()
                    val requestBody = body.toRequestBody(mediaType)
                    requestBuilder.put(requestBody)
                }
                else -> requestBuilder.get()
            }

            // Build and execute request
            val request = requestBuilder.build()
            client.newCall(request).enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    Log.d("RobosatsError", e.toString())
                    rejectPromise(uuid, "Request failed: ${e.message}")
                }

                override fun onResponse(call: Call, response: Response) {
                    try {
                        // Get response body
                        val responseBody = response.body.string()

                        // Create JSON object with headers
                        val headersJson = JSONObject()
                        response.headers.names().forEach { name ->
                            headersJson.put(name, response.header(name))
                        }

                        val result = "{\"json\":$responseBody, \"headers\": $headersJson}"
                        resolvePromise(uuid, result)
                    } catch (e: Exception) {
                        Log.e(TAG, "Error processing response", e)
                        rejectPromise(uuid, "Error processing response: ${e.message}")
                    }
                }
            })
        } catch (e: Exception) {
            Log.e(TAG, "Error in sendRequest", e)
            rejectPromise(uuid, "Error sending request: ${e.message}")
        }
    }

    @JavascriptInterface
    fun getEncryptedStorage(uuid: String, key: String) {
        // Validate inputs before processing
        if (!isValidUuid(uuid) || !isValidInput(key)) {
            Log.e(TAG, "Invalid input for getEncryptedStorage: uuid=$uuid, key=$key")
            rejectPromise(uuid, "Invalid input parameters")
            return
        }

        try {
            // Sanitize the input before passing to native code
            val sanitizedKey = key.trim()

            val value = EncryptedStorage.getEncryptedStorage(sanitizedKey)

            // Safely encode and return the result
            resolvePromise(uuid, value)
        } catch (e: Exception) {
            Log.e(TAG, "Error in getEncryptedStorage", e)
            rejectPromise(uuid, "Error obtaining encrypted storage: $key")
        }
    }

    @JavascriptInterface
    fun setEncryptedStorage(uuid: String, key: String, value: String) {
        // Validate inputs before processing
        if (!isValidUuid(uuid) || !isValidInput(key)) {
            Log.e(TAG, "Invalid input for setEncryptedStorage: uuid=$uuid, key=$key")
            rejectPromise(uuid, "Invalid input parameters")
            return
        }
        // Sanitize the input before passing to native code
        val sanitizedKey = key.trim()
        val sanitizedValue = value.trim()

        EncryptedStorage.setEncryptedStorage(sanitizedKey, sanitizedValue)

        if (key == "garage_slots") NostrClient.refresh()
        if (key == "settings_notifications") {
            val serviceIntent = Intent(context, NotificationsService::class.java)
            if (value == "true") {
                context.startForegroundService(serviceIntent)
            } else {
                context.stopService(serviceIntent)
            }
        }

        // Safely encode and return the result
        resolvePromise(uuid, key)
    }

    @JavascriptInterface
    fun deleteEncryptedStorage(uuid: String, key: String) {
        // Validate inputs before processing
        if (!isValidUuid(uuid) || !isValidInput(key)) {
            Log.e(TAG, "Invalid input for deleteEncryptedStorage: uuid=$uuid, key=$key")
            rejectPromise(uuid, "Invalid input parameters")
            return
        }
        // Sanitize the input before passing to native code
        val sanitizedKey = key.trim()

        EncryptedStorage.deleteEncryptedStorage(sanitizedKey)

        // Safely encode and return the result
        resolvePromise(uuid, key)
    }

    /** 0.9.3: keep the screen on while the order page waits for an onchain signature of this
     * app (the signing only runs while the order is open in the foreground). */
    @JavascriptInterface
    fun keepScreenOn(on: Boolean) {
        context.runOnUiThread {
            try {
                if (on) {
                    context.window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                } else {
                    context.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                }
            } catch (e: Exception) {
                Log.e(TAG, "keepScreenOn", e)
            }
        }
    }

    // 0.9.6: recent logs for a bug report. The page redacts tokens, keys, PGP blocks and
    // invoices before it shows, copies or shares anything (utils/logs.ts).
    @JavascriptInterface
    fun getRecentLogs(uuid: String) {
        if (!isValidUuid(uuid)) return
        Thread {
            val logs = runCatching {
                val p = Runtime.getRuntime().exec(arrayOf("logcat", "-d", "-t", "800", "-v", "time", "--pid=${android.os.Process.myPid()}"))
                p.inputStream.bufferedReader().use { it.readText() }
            }.getOrElse { "logcat unavailable: ${it.message}" }
            val crash = runCatching { java.io.File(context.filesDir, "last_crash.txt").takeIf { it.exists() }?.readText() }.getOrNull()
            val o = JSONObject().put("logs", logs).put("crash", crash ?: "")
                .put("version", appVersion()).put("android", android.os.Build.VERSION.RELEASE)
                .put("device", "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}")
            resolvePromise(uuid, o.toString())
        }.start()
    }

    /** Android's share sheet with [text] (already redacted by the page). */
    @JavascriptInterface
    fun shareText(text: String) {
        context.runOnUiThread {
            runCatching {
                val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text)
                context.startActivity(Intent.createChooser(send, "Roblosats logs").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
        }
    }

    // 0.9.6: background reliability. Notifications need POST_NOTIFICATIONS (Android 13+), and
    // some phones stop the notification service unless the app is exempt from battery
    // optimization. Asked with an explanation, never forced.
    @JavascriptInterface
    fun backgroundStatus(uuid: String) {
        if (!isValidUuid(uuid)) return
        val notif = if (android.os.Build.VERSION.SDK_INT >= 33)
            androidx.core.content.ContextCompat.checkSelfPermission(context, android.Manifest.permission.POST_NOTIFICATIONS) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED
        else androidx.core.app.NotificationManagerCompat.from(context).areNotificationsEnabled()
        val pm = context.getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
        val exempt = pm.isIgnoringBatteryOptimizations(context.packageName)
        resolvePromise(uuid, JSONObject().put("notifications", notif).put("batteryExempt", exempt).toString())
    }

    @JavascriptInterface
    fun requestNotificationPermission() {
        context.runOnUiThread {
            runCatching {
                if (android.os.Build.VERSION.SDK_INT >= 33 &&
                    androidx.core.app.ActivityCompat.shouldShowRequestPermissionRationale(context, android.Manifest.permission.POST_NOTIFICATIONS).not() &&
                    EncryptedStorage.getEncryptedStorage("notif_permission_asked") == "true"
                ) {
                    // Android will not ask again: open the app's notification settings
                    context.startActivity(Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                        .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, context.packageName))
                } else if (android.os.Build.VERSION.SDK_INT >= 33) {
                    EncryptedStorage.setEncryptedStorage("notif_permission_asked", "true")
                    androidx.core.app.ActivityCompat.requestPermissions(context, arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1001)
                } else {
                    context.startActivity(Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                        .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, context.packageName))
                }
            }
        }
    }

    @android.annotation.SuppressLint("BatteryLife")
    @JavascriptInterface
    fun requestBatteryExemption() {
        context.runOnUiThread {
            runCatching {
                context.startActivity(Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    android.net.Uri.parse("package:${context.packageName}")))
            }.onFailure {
                runCatching { context.startActivity(Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
            }
        }
    }

    /** 0.9.7: the order a tapped notification asked to open, once; "" when none. */
    @JavascriptInterface
    fun takePendingNavigation(): String {
        context.pageReady = true
        val nav = context.pendingNavigation
        context.pendingNavigation = ""
        return nav
    }

    // 0.9.6: in-app update (as in Kilowallet). The check goes to GitHub through Tor when the
    // app uses its own Tor; nothing is installed unless the APK's SHA-256 matches the release
    // notes and it carries this app's signing certificate.
    // 0.9.13: waits for Tor (callers run in their own Thread), never "no proxy" while it starts
    private fun updateProxy(): java.net.Proxy? = if (context.useProxy) TorGate.awaitProxy() else null

    @JavascriptInterface
    fun checkUpdate(uuid: String) {
        if (!isValidUuid(uuid)) return
        Thread {
            val result = com.robosats.updates.AppUpdater.check(context, updateProxy())
            resolvePromise(uuid, result)
        }.start()
    }

    @JavascriptInterface
    fun startUpdate() {
        com.robosats.updates.AppUpdater.onState = { json ->
            safeEvaluateJavascript("javascript:window.AndroidRobosats.onUpdateState('${encodeForJavaScript(json)}')")
        }
        Thread { com.robosats.updates.AppUpdater.start(context, updateProxy()) }.start()
    }

    /** After "Allow from this source": opens Android's setting, then the page calls resumeUpdate. */
    @JavascriptInterface
    fun allowInstalls() {
        context.runOnUiThread {
            runCatching {
                val i = Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    android.net.Uri.parse("package:${context.packageName}"))
                context.startActivity(i)
            }
        }
    }

    @JavascriptInterface
    fun resumeUpdate() {
        Thread { com.robosats.updates.AppUpdater.resume(context) }.start()
    }

    /** Opens a Roblosats release page in the browser (only github.com/Kilombino/roblosats). */
    @JavascriptInterface
    fun openReleasePage(url: String) {
        if (!url.startsWith("https://github.com/Kilombino/roblosats/")) return
        context.runOnUiThread {
            runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
        }
    }

    /** The installed app's version name ("0.9.6"). */
    @JavascriptInterface
    fun appVersion(): String =
        runCatching { context.packageManager.getPackageInfo(context.packageName, 0).versionName }.getOrNull() ?: ""

    @JavascriptInterface
    fun restart() {
        try {
            Log.d(TAG, "Restarting app...")

            val intent = context.packageManager.getLaunchIntentForPackage(context.packageName)
            intent?.let {
                it.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                it.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TASK)

                context.startActivity(it)
                context.finish()
            } ?: run {
                Log.e(TAG, "Could not get launch intent for app restart")
                Toast.makeText(context, "Failed to restart app", Toast.LENGTH_SHORT).show()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error restarting app", e)
            Toast.makeText(context, "Failed to restart app", Toast.LENGTH_SHORT).show()
        }
    }

    /**
     * 0.9.11: Clearnet mode (beta). Built-in Tor (or Orbot) stays the default; with Clearnet on,
     * a coordinator that publishes a clearnet address is reached directly and every .onion keeps
     * going through Tor. Read per request, so a switch applies at once.
     */
    private fun useTorFor(url: String): Boolean {
        if (!context.useProxy) return false
        if (!context.clearnet) return true
        val host = try { Uri.parse(url).host?.lowercase() ?: "" } catch (e: Exception) { "" }
        // only the coordinators' own clearnet hosts go direct; everything else (update check,
        // explorer, uploads, other sites) keeps going through Tor
        val tor = host.endsWith(".onion") || host !in context.clearnetHosts
        Log.d(TAG, "clearnet mode: $host via ${if (tor) "tor" else "direct"}")
        return tor
    }

    /** 0.9.11: Clearnet mode, read synchronously by the page while it starts (so its first
     * connection already uses the clearnet addresses instead of the onions). */
    @JavascriptInterface
    fun isClearnet(): Boolean = context.clearnet

    /** 0.9.11: change Tor (built-in or Orbot) and Clearnet in place, without restarting the app
     * (the restart showed a black screen). Starting the built-in Tor runs in the background. */
    @JavascriptInterface
    fun setNetworkMode(useProxy: Boolean, clearnet: Boolean, hosts: String) {
        EncryptedStorage.setEncryptedStorage("settings_use_proxy", useProxy.toString())
        EncryptedStorage.setEncryptedStorage("settings_clearnet", clearnet.toString())
        EncryptedStorage.setEncryptedStorage("settings_clearnet_hosts", hosts)
        context.clearnetHosts = MainActivity.parseHosts(hosts)
        context.clearnet = clearnet
        if (useProxy && !context.useProxy) {
            Thread {
                try {
                    context.startBuiltInTor()
                } catch (e: Exception) {
                    Log.e(TAG, "Could not start the built-in Tor: ${e.message}")
                }
            }.start()
        }
        context.useProxy = useProxy
        // 0.9.13: notification relays follow (Orbot: no proxy; built-in: Tor's, or none until ready)
        if (useProxy) {
            TorGate.onTorProxyChanged(TorGate.readyProxy())
        } else {
            com.vitorpamplona.ammolite.service.HttpClientManager.setDefaultProxy(null)
        }
    }

    private fun onWsMessage(path: String?, message: String?) {
        val encodedMessage = encodeForJavaScript(message)
        safeEvaluateJavascript("javascript:window.AndroidRobosats.onWSMessage('$path', '$encodedMessage')")
    }

    private fun onWsError(path: String?) {
        Log.d(TAG, "WebSocket error: $path")
        safeEvaluateJavascript("javascript:window.AndroidRobosats.onWsError('$path')")
    }

    private fun onWsClose(path: String?) {
        Log.d(TAG, "WebSocket close: $path")
        safeEvaluateJavascript("javascript:window.AndroidRobosats.onWsClose('$path')")
    }

    private fun resolvePromise(uuid: String, result: String) {
        if (!isValidUuid(uuid)) {
            Log.e(TAG, "Invalid UUID for promise resolution: $uuid")
            return
        }

        val encodedResult = encodeForJavaScript(result)
        safeEvaluateJavascript("javascript:window.AndroidRobosats.onResolvePromise('$uuid', '$encodedResult')")
    }

    private fun rejectPromise(uuid: String, errorMessage: String) {
        if (!isValidUuid(uuid)) {
            Log.e(TAG, "Invalid UUID for promise rejection: $uuid")
            return
        }

        val encodedError = encodeForJavaScript(errorMessage)
        safeEvaluateJavascript("javascript:window.AndroidRobosats.onRejectPromise('$uuid', '$encodedError')")
    }

    /**
     * Validates that a URL is safe to request from the native HTTP client.
     * Accepts only http/https/ws/wss schemes and rejects private/loopback addresses
     * (which would enable SSRF against local services on the device or local network).
     * All coordinator traffic is routed through the Tor SOCKS proxy, so .onion hosts
     * with http/https are legitimate.
     *
     * NOTE: java.net.URL has no protocol handler for ws/wss and throws
     * MalformedURLException for those schemes.  We normalise ws→http and wss→https
     * solely for the purpose of parsing host/port; the original scheme is checked
     * against the allowlist first so the SSRF protections remain intact.
     */
    private fun isValidUrl(rawUrl: String?): Boolean {
        if (rawUrl.isNullOrBlank()) return false
        return try {
            val trimmed = rawUrl.trim()
            val scheme = trimmed.substringBefore("://").lowercase()
            if (scheme !in setOf("http", "https", "ws", "wss")) return false
            // Normalise ws→http / wss→https so java.net.URL can parse the host/port.
            val normalized = when (scheme) {
                "ws"  -> "http://" + trimmed.removePrefix("ws://")
                "wss" -> "https://" + trimmed.removePrefix("wss://")
                else  -> trimmed
            }
            val url = URL(normalized)
            val host = url.host.lowercase()
            // Block loopback and link-local addresses to prevent SSRF
            if (host == "localhost") return false
            val ipv4Loopback = Regex("""^127\.\d+\.\d+\.\d+$""")
            val ipv4Private = Regex("""^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.)""")
            val ipv4LinkLocal = Regex("""^169\.254\.""")
            if (ipv4Loopback.containsMatchIn(host)) return false
            if (ipv4Private.containsMatchIn(host)) return false
            if (ipv4LinkLocal.containsMatchIn(host)) return false
            if (host == "::1" || host == "[::1]") return false
            true
        } catch (e: MalformedURLException) {
            false
        }
    }

    private fun isValidInput(input: String?): Boolean {
        if (input == null || input.isEmpty()) {
            return false
        }
        return SAFE_STRING_PATTERN.matcher(input).matches()
    }

    private fun isValidUuid(uuid: String?): Boolean {
        if (uuid == null || uuid.isEmpty()) {
            return false
        }
        return UUID_PATTERN.matcher(uuid).matches()
    }

    private fun safeEvaluateJavascript(script: String) {
        // Remove any null bytes which could be used to trick the JS interpreter
        val sanitizedScript = script.replace("\u0000", "")

        webView.post {
            try {
                webView.evaluateJavascript(sanitizedScript, null)
            } catch (e: Exception) {
                Log.e(TAG, "Error evaluating JavaScript: $e")
            }
        }
    }

    private fun encodeForJavaScript(input: String?): String {
        if (input == null) return ""

        return input.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\"", "\\\"")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
            .replace("<", "\\u003C")
            .replace(">", "\\u003E")
            .replace("&", "\\u0026")
    }

}
