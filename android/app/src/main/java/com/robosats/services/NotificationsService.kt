package com.robosats.services
import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.IBinder
import android.os.PowerManager
import android.util.Base64
import android.util.Log
import androidx.annotation.RequiresPermission
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationChannelGroupCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.graphics.createBitmap
import com.robosats.Connectivity
import com.robosats.MainActivity
import com.robosats.R
import com.robosats.RoboIdentities
import com.robosats.models.EncryptedStorage
import com.robosats.models.NostrClient
import com.robosats.models.NostrClient.getRobotKeyPair
import com.robosats.models.NotificationDedup
import com.robosats.models.RobotKeys
import com.robosats.models.SeenWraps
import com.robosats.tor.TorGate
import com.vitorpamplona.ammolite.relays.Client
import com.vitorpamplona.ammolite.relays.Relay
import com.vitorpamplona.quartz.events.ChatMessageEvent
import com.vitorpamplona.quartz.events.Event
import com.vitorpamplona.quartz.events.GiftWrapEvent
import com.vitorpamplona.quartz.events.SealedGossipEvent
import com.vitorpamplona.quartz.signers.NostrSignerInternal
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.Timer
import java.util.TimerTask
import java.util.concurrent.ConcurrentHashMap

class NotificationsService : Service() {
    private var channelRelaysId = "RelaysConnections"
    private var channelNotificationsId = "Notifications"
    // 0.9.4: steps where the robot must act (the coordinator tags them "action")
    private var channelActionId = "OrderNeedsYou"

    private lateinit var notificationGroup: NotificationChannelGroupCompat
    private var wakeLock: PowerManager.WakeLock? = null

    private val roboIdentities = RoboIdentities()
    private val timer = Timer()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val processedEvents = ConcurrentHashMap<String, Boolean>()
    private val dedupStore = object : NotificationDedup.Store {
        override fun get(key: String) = EncryptedStorage.getEncryptedStorage(key)
        override fun set(key: String, value: String) = EncryptedStorage.setEncryptedStorage(key, value)
    }

    companion object {
        const val ACTION_STOP_SERVICE = "com.robosats.services.STOP_SERVICE"
    }

    // 0.9.13: gift wraps already opened, kept on the device (flushed once a minute)
    private val seenWraps by lazy { SeenWraps(EncryptedStorage.getEncryptedStorage(SeenWraps.KEY)) }

    private val clientNotificationListener =
        object : Client.Listener {
            override fun onEvent(
                event: Event,
                subscriptionId: String,
                relay: Relay,
                afterEOSE: Boolean,
            ) {
                // 0.9.13: this runs in a library coroutine without an exception handler: anything
                // thrown here (a bad robot key, a malformed event, storage) killed the app. One bad
                // event or robot is now logged and skipped.
                try {
                    handleEvent(event, subscriptionId, relay)
                } catch (t: Throwable) {
                    Log.e("RobosatsNotifications", "Event ${event.id.take(8)}… from ${relay.url} skipped: ${t.message}", t)
                }
            }
        }

    private fun handleEvent(event: Event, subscriptionId: String, relay: Relay) {
        if (event !is GiftWrapEvent || processedEvents.containsKey(event.id)) return
        val firstTaggedUser = event.firstTaggedUser()?.lowercase() ?: return
        // one snapshot of the garage for the robot list and the key (the page may rewrite it
        // in between, robot by robot, while it loads)
        val garageString = EncryptedStorage.getEncryptedStorage("garage_slots")
        if (firstTaggedUser !in RobotKeys.pubKeys(garageString)) return
        val keyPair = getRobotKeyPair(firstTaggedUser, garageString) ?: return
        // seen only once it could really be opened: a robot whose key was not readable yet gets
        // its gift wraps again on the next subscription
        processedEvents[event.id] = true
        if (!seenWraps.firstTime(event.id, event.createdAt)) return
        Log.d("RobosatsNotifications", "Relay Event: ${relay.url} - $subscriptionId - ${event.id}")

        val nostrSigner = NostrSignerInternal(keyPair)
        event.unwrap(nostrSigner) { gift ->
            try {
                if (gift is SealedGossipEvent) {
                    gift.unseal(nostrSigner) { rumor ->
                        try {
                            if (rumor is ChatMessageEvent) {
                                val federationPubKeys = EncryptedStorage.getEncryptedStorage("federation_pubkeys")
                                // 0.9.6: each notice once, ever (rumor ids kept on the device): a relay that
                                // sends the same gift wrap again after a restart no longer repeats it
                                if (federationPubKeys.contains(rumor.pubKey) &&
                                    NotificationDedup.shouldNotify(dedupStore, rumor.id, rumor.createdAt)
                                ) {
                                    displayOrderNotification(rumor, firstTaggedUser)
                                }
                            }
                        } catch (t: Throwable) {
                            Log.e("RobosatsNotifications", "Notice of ${event.id.take(8)}… skipped: ${t.message}", t)
                        }
                    }
                }
            } catch (t: Throwable) {
                Log.e("RobosatsNotifications", "Seal of ${event.id.take(8)}… skipped: ${t.message}", t)
            }
        }
    }

    private fun flushSeenWraps() {
        try {
            if (seenWraps.dirty) EncryptedStorage.setEncryptedStorage(SeenWraps.KEY, seenWraps.serialize())
        } catch (t: Throwable) {
            Log.w("RobosatsNotifications", "Could not save seen gift wraps: ${t.message}")
        }
    }

    private val networkCallback =
        object : ConnectivityManager.NetworkCallback() {
            var lastNetwork: Network? = null

            override fun onAvailable(network: Network) {
                super.onAvailable(network)

                if (lastNetwork != null && lastNetwork != network) {
                    scope.launch(Dispatchers.IO) {
                        stopSubscription()
                        delay(1000)
                        startSubscription()
                    }
                }

                lastNetwork = network
            }

            // Network capabilities have changed for the network
            override fun onCapabilitiesChanged(
                network: Network,
                networkCapabilities: NetworkCapabilities,
            ) {
                super.onCapabilitiesChanged(network, networkCapabilities)

                scope.launch(Dispatchers.IO) {
                    Log.d(
                        "RobosatsNotifications",
                        "onCapabilitiesChanged: ${network.networkHandle} hasMobileData ${Connectivity.isOnMobileData} hasWifi ${Connectivity.isOnWifiData}",
                    )
                    if (Connectivity.updateNetworkCapabilities(networkCapabilities)) {
                        stopSubscription()
                        delay(1000)
                        startSubscription()
                    }
                }
            }
        }

    override fun onBind(intent: Intent): IBinder {
        return null!!
    }

    override fun onCreate() {
        try {
            // Acquire a partial wake lock to keep CPU running when screen is off
            val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = powerManager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "RoboSats::NotificationsWakeLock"
            ).apply {
                acquire()
                Log.d("RobosatsNotifications", "Wake lock acquired")
            }

            val connectivityManager =
                (getSystemService(ConnectivityManager::class.java) as ConnectivityManager)
            connectivityManager.registerDefaultNetworkCallback(networkCallback)

            // Initialize NostrClient safely
            NostrClient.init()

            super.onCreate()
        } catch (e: Exception) {
            Log.e("NotificationsService", "Error in onCreate", e)
            // Call super.onCreate() even if there's an error to ensure proper service lifecycle
            super.onCreate()
        }
    }

    @RequiresPermission(Manifest.permission.POST_NOTIFICATIONS)
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Handle stop action
        if (intent?.action == ACTION_STOP_SERVICE) {
            Log.d("RobosatsNotifications", "Received stop service action")
            stopSelf()
            return START_NOT_STICKY
        }

        startService()

        return START_STICKY
    }

    override fun onDestroy() {
        timer.cancel()
        flushSeenWraps()
        stopSubscription()

        try {
            val connectivityManager =
                (getSystemService(ConnectivityManager::class.java) as ConnectivityManager)
            connectivityManager.unregisterNetworkCallback(networkCallback)
        } catch (e: Exception) {
            Log.d("RobosatsNotifications", "Failed to unregisterNetworkCallback", e)
        }

        // Release wake lock
        wakeLock?.let {
            if (it.isHeld) {
                it.release()
                Log.d("RobosatsNotifications", "Wake lock released")
            }
        }

        super.onDestroy()
    }

    @RequiresPermission(Manifest.permission.POST_NOTIFICATIONS)
    private fun startService() {
        try {
            Log.d("RobosatsNotifications", "Starting foreground service...")
            startForeground(1, createNotification())
            keepAlive()

            startSubscription()

            val connectivityManager =
                (getSystemService(ConnectivityManager::class.java) as ConnectivityManager)
            connectivityManager.registerDefaultNetworkCallback(networkCallback)
        } catch (e: Exception) {
            Log.e("NotificationsService", "Error in service", e)
        }
    }

    // 0.9.13: a subscription waiting for Tor must not start after the service was stopped
    @Volatile
    private var subscribed = false

    private fun startSubscription() {
        subscribed = true
        if (!Client.isSubscribed(clientNotificationListener)) Client.subscribe(clientNotificationListener)

        CoroutineScope(Dispatchers.IO).launch {
            try {
                // 0.9.13: relays are never dialled without Tor's SOCKS proxy in Tor/Clearnet mode
                // (no direct connection, no system DNS for a .onion); if the app is not open, the
                // service starts the built-in Tor itself
                val readyNow = TorGate.prepareRelays(application) {
                    // fresh relays (a failed one waits 180 s before retrying)
                    if (subscribed) {
                        NostrClient.stop()
                        NostrClient.start()
                    }
                }
                if (readyNow) NostrClient.start()
            } catch (t: Throwable) {
                Log.e("RobosatsNotifications", "startSubscription: ${t.message}", t)
            }
        }
    }

    private fun stopSubscription() {
        subscribed = false
        Client.unsubscribe(clientNotificationListener)
        NostrClient.stop()
    }

    private fun keepAlive() {
        timer.schedule(
            object : TimerTask() {
                override fun run() {
                    // 0.9.13: a TimerTask that throws cancels the Timer for good
                    try {
                        // 0.9.13: no relay is dialled before Tor is ready (see TorGate)
                        if (TorGate.relaysCanConnect()) NostrClient.checkRelaysHealth()
                        NostrClient.markActive()
                        flushSeenWraps()
                    } catch (t: Throwable) {
                        Log.e("RobosatsNotifications", "keepAlive: ${t.message}", t)
                    }
                }
            },
            5000,
            61000,
        )
    }

    @RequiresPermission(Manifest.permission.POST_NOTIFICATIONS)
    private fun createNotification(): Notification {
        val notificationManager = NotificationManagerCompat.from(this)

        Log.d("RobosatsNotifications", "Building groups...")
        notificationGroup = NotificationChannelGroupCompat.Builder("ServiceGroup")
            .setName(getString(R.string.notifications))
            .setDescription(getString(R.string.robosats_is_running_in_background))
            .build()

        notificationManager.createNotificationChannelGroup(notificationGroup)

        Log.d("RobosatsNotifications", "Building channels...")
        val channelRelays = NotificationChannelCompat.Builder(channelRelaysId, NotificationManager.IMPORTANCE_DEFAULT)
            .setName(getString(R.string.service))
            .setGroup(notificationGroup.id)
            .build()

        val channelNotification = NotificationChannelCompat.Builder(channelNotificationsId, NotificationManager.IMPORTANCE_HIGH)
            .setName(getString(R.string.notifications))
            .setGroup(notificationGroup.id)
            .build()

        val channelAction = NotificationChannelCompat.Builder(channelActionId, NotificationManager.IMPORTANCE_HIGH)
            .setName(getString(R.string.order_needs_you))
            .setDescription(getString(R.string.order_needs_you_description))
            .setVibrationEnabled(true)
            .setVibrationPattern(longArrayOf(0, 300, 150, 300))
            .setGroup(notificationGroup.id)
            .build()

        notificationManager.createNotificationChannel(channelRelays)
        notificationManager.createNotificationChannel(channelNotification)
        notificationManager.createNotificationChannel(channelAction)

        Log.d("RobosatsNotifications", "Building notification...")

        // Create intent to stop the service
        val stopIntent = Intent(this, NotificationsService::class.java).apply {
            action = ACTION_STOP_SERVICE
            // Restrict delivery to this app's package to prevent implicit intent interception
            setPackage(packageName)
        }
        val stopPendingIntent = PendingIntent.getService(
            this,
            0,
            stopIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        // 0.9.7: tapping "Roblosats is running" opens the app
        val openIntent = Intent(this, MainActivity::class.java).apply {
            action = Intent.ACTION_MAIN
            addCategory(Intent.CATEGORY_LAUNCHER)
            setPackage(packageName)
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED or Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val openPendingIntent = PendingIntent.getActivity(
            this,
            2,
            openIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

        val notificationBuilder =
            NotificationCompat.Builder(this, channelRelaysId)
                .setContentTitle(getString(R.string.robosats_is_running_in_background))
                .setContentIntent(openPendingIntent)
                .setPriority(NotificationCompat.PRIORITY_MIN)
                .setGroup(notificationGroup.id)
                .setSmallIcon(R.drawable.ic_notification)
                .setOngoing(true)  // Make notification persistent
                .addAction(
                    R.drawable.ic_notification,
                    "Stop",  // You may want to add this to strings.xml for localization
                    stopPendingIntent
                )

        val build = notificationBuilder.build()
        notificationManager.notify(1, build)
        return build
    }

    private fun displayOrderNotification(event: ChatMessageEvent, hexPubKey: String) {
        val notificationManager =
            getSystemService(NOTIFICATION_SERVICE) as NotificationManager

        val orderId = event.firstTag("order_id")

        val intent = Intent(applicationContext, MainActivity::class.java).apply {
            action = "com.robosats.OPEN_ORDER"
            putExtra("order_id", orderId)
            // 0.9.7: the robot the notice is for, so the app opens the order with that robot
            putExtra("robot", hexPubKey)
            // the open app gets it in onNewIntent instead of a second copy of the activity
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
            // Restrict delivery to this app's package to prevent implicit intent interception
            setPackage(applicationContext.packageName)
        }
        val pendingIntent = PendingIntent.getActivity(
            applicationContext,
            // Use a unique request code per notification so each gets its own PendingIntent
            // extras (order_id) rather than sharing a single reused PendingIntent
            event.id.hashCode(),
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val garageString = EncryptedStorage.getEncryptedStorage("garage_slots")
        var bitmap: Bitmap? = null

        if (garageString.isNotEmpty()) {
            val garage = JSONObject(garageString)
            val keys = garage.keys()
            while (keys.hasNext() && bitmap == null) {
                val key = keys.next()
                val slot = garage.getJSONObject(key)
                val slotHexPubKey = slot.getString("nostrPubKey")
                if (hexPubKey.isNotEmpty() && hexPubKey == slotHexPubKey) {
                    val hashId = slot.getString("hashId")
                    if (hashId.isNotEmpty()) {
                        val base64Image = roboIdentities.generateRobohash("$hashId;80")
                        val imageBytes = Base64.decode(base64Image, Base64.DEFAULT)
                        val rawBitmap = BitmapFactory.decodeByteArray(imageBytes, 0, imageBytes.size)
                        bitmap = getRoundedBitmap(rawBitmap)
                    }
                }
            }
        }

        val needsYou = event.firstTag("action") == "1"
        val builder: NotificationCompat.Builder =
            NotificationCompat.Builder(
                applicationContext,
                if (needsYou) channelActionId else channelNotificationsId,
            )
                .setContentTitle(orderId?.replace("/", "#")?.replaceFirstChar { it.uppercase() })
                .setContentText(event.content)
                // 0.9.4: the whole step text, not one cut line
                .setStyle(NotificationCompat.BigTextStyle().bigText(event.content))
                .setSmallIcon(R.drawable.ic_notification)
                .setLargeIcon(bitmap)
                .setContentIntent(pendingIntent)
                .setPriority(if (needsYou) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
                .setCategory(if (needsYou) NotificationCompat.CATEGORY_REMINDER else NotificationCompat.CATEGORY_MESSAGE)
                .setAutoCancel(true)
        if (needsYou) builder.setVibrate(longArrayOf(0, 300, 150, 300))

        notificationManager.notify(event.id.hashCode(), builder.build())
    }

    private fun getRoundedBitmap(bitmap: Bitmap): Bitmap {
        val output = createBitmap(bitmap.width, bitmap.height)
        val canvas = Canvas(output)
        val paint = Paint()
        val path = Path()

        // Create a rounded rectangle path
        path.addRoundRect(0f, 0f, bitmap.width.toFloat(), bitmap.height.toFloat(),
            bitmap.width / 2f, bitmap.height / 2f, Path.Direction.CW)

        canvas.clipPath(path)
        canvas.drawBitmap(bitmap, 0f, 0f, paint)

        return output
    }
}
