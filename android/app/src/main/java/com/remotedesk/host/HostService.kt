package com.remotedesk.host

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ContentValues
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.PixelFormat
import android.graphics.drawable.Icon
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.provider.MediaStore
import android.util.DisplayMetrics
import android.view.WindowManager
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.OutputStream
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Foreground service that makes this phone controllable:
 *  - keeps an outgoing connection to the relay (no fixed IP needed)
 *  - authenticates viewers (password / saved device / on-screen approval)
 *  - streams the screen (MediaProjection) as encrypted JPEG frames
 *  - forwards mouse/keyboard events to ControlService (accessibility)
 */
class HostService : Service() {

    class PendingReq(val id: String, val hn: String, val vn: String, val key: ByteArray,
                     val device: String, val name: String, val ip: String, val code: String)

    companion object {
        @Volatile var instance: HostService? = null
        @Volatile var statusText: String = "לא פעיל"
        @Volatile var online: Boolean = false
        @Volatile var sessionIp: String? = null
        @Volatile var listener: (() -> Unit)? = null

        const val CH_STATUS = "status"
        const val CH_ALERT = "alert"
        const val NOTIF_ID = 1
        const val REQ_NOTIF_ID = 2
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        const val ACTION_STOP = "com.remotedesk.host.STOP"
        const val ACTION_KICK = "com.remotedesk.host.KICK"

        fun notifyListener() { listener?.invoke() }
    }

    lateinit var cfg: Config
    private val exec = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val http = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    // connection / protocol state (touched only on `exec`)
    private var ws: WebSocket? = null
    private var stopped = false
    private var backoff = 1000L
    @Volatile private var chan: SecureChannel? = null
    private var pendingNonce: String? = null
    private var peerIp = "?"
    @Volatile var pendingReq: PendingReq? = null
    private val failures = ArrayList<Long>()
    private val requests = ArrayList<Long>()

    // screen capture
    private var projection: MediaProjection? = null
    private var vdisplay: VirtualDisplay? = null
    private var reader: ImageReader? = null
    @Volatile var screenW = 1
    @Volatile var screenH = 1
    private val capThread = HandlerThread("capture").apply { start() }
    private val capHandler = Handler(capThread.looper)
    private val lock = Object()
    @Volatile private var sessionActive = false
    @Volatile private var force = false
    private var inFlight = 0
    @Volatile private var quality = 60
    private var streamer: Thread? = null

    // file receive
    private var fileUri: Uri? = null
    private var fileOut: OutputStream? = null
    private var fileName = ""
    private var fileRemaining = 0L

    val hasProjection: Boolean get() = projection != null

    // ------------------------------------------------------------------ lifecycle
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        cfg = Config(this)
        createChannels()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopEverything()
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_KICK) {
            exec.execute { kick() }
            return START_NOT_STICKY
        }
        startForeground(NOTIF_ID, statusNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        val data: Intent? = if (Build.VERSION.SDK_INT >= 33)
            intent?.getParcelableExtra(EXTRA_DATA, Intent::class.java)
        else @Suppress("DEPRECATION") intent?.getParcelableExtra(EXTRA_DATA)
        val code = intent?.getIntExtra(EXTRA_CODE, 0) ?: 0
        if (data != null) {
            try {
                startProjection(code, data)
            } catch (e: Exception) {
                status("שיתוף המסך נכשל: ${e.message}", online)
            }
        }
        stopped = false
        exec.execute { if (ws == null) connect() }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopEverything()
        instance = null
        super.onDestroy()
    }

    private fun stopEverything() {
        stopped = true
        exec.execute {
            endSession()
            ws?.close(1000, "stop")
            ws = null
        }
        projection?.stop()
        projection = null
        online = false
        status("כבוי", false)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    /** Settings changed (server / password / id): reconnect. */
    fun reconnect() {
        exec.execute {
            endSession()
            ws?.cancel()
        }
    }

    // ------------------------------------------------------------------ status
    private fun status(text: String, ok: Boolean) {
        statusText = text
        online = ok
        main.post {
            getSystemService(NotificationManager::class.java).notify(NOTIF_ID, statusNotification())
            notifyListener()
        }
    }

    private fun createChannels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_STATUS, "מצב RemoteDesk", NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CH_ALERT, "בקשות גישה", NotificationManager.IMPORTANCE_HIGH).apply {
            enableVibration(true)
        })
    }

    private fun pi(intent: Intent, req: Int, activity: Boolean = false): PendingIntent {
        val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        return if (activity) PendingIntent.getActivity(this, req, intent, flags)
        else PendingIntent.getBroadcast(this, req, intent, flags)
    }

    private fun statusNotification(): Notification {
        val open = pi(Intent(this, MainActivity::class.java), 10, activity = true)
        val b = Notification.Builder(this, CH_STATUS)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentIntent(open)
            .setOngoing(true)
        val ip = sessionIp
        if (ip != null) {
            b.setContentTitle("⚠ הטלפון נשלט מרחוק")
                .setContentText("חיבור מ-$ip")
                .setColor(Color.rgb(198, 40, 40))
                .addAction(Notification.Action.Builder(
                    Icon.createWithResource(this, R.drawable.ic_stat), "נתק",
                    pi(Intent(this, ActionReceiver::class.java).setAction(ACTION_KICK), 11)).build())
        } else {
            b.setContentTitle("RemoteDesk · ${Config.formatId(cfg.id)}")
                .setContentText(statusText)
        }
        return b.build()
    }

    // ------------------------------------------------------------------ connection
    private fun connect() {
        if (stopped) return
        status("מתחבר לשרת...", false)
        val req = try {
            Request.Builder().url(cfg.server).build()
        } catch (e: Exception) {
            status("כתובת שרת לא תקינה", false)
            return
        }
        ws = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(JSONObject().put("type", "register").put("id", cfg.id)
                    .put("secret", cfg.secret).toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                exec.execute { if (webSocket === ws) onText(text) }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                val b = bytes.toByteArray()
                exec.execute { if (webSocket === ws) onBinary(b) }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                exec.execute { if (webSocket === ws) disconnected() }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                exec.execute { if (webSocket === ws) disconnected() }
            }
        })
    }

    private fun disconnected() {
        ws = null
        endSession()
        if (stopped) return
        status("לא מחובר לשרת - מנסה שוב...", false)
        val delay = backoff
        backoff = minOf(backoff * 2, 30_000L)
        main.postDelayed({ exec.execute { if (ws == null) connect() } }, delay)
    }

    private fun sendText(o: JSONObject) {
        ws?.send(o.toString())
    }

    private fun onText(text: String) {
        val m = try { JSONObject(text) } catch (e: Exception) { return }
        when (m.optString("type")) {
            "registered" -> {
                backoff = 1000L
                status(if (projection == null) "מחובר · יש להפעיל שיתוף מסך" else "מוכן - ממתין לחיבורים", true)
            }
            "error" -> {
                if (m.optString("code") == "id_taken") {
                    cfg.id = Config.newId()
                    cfg.clearPassword()
                }
                ws?.cancel()
            }
            "peer_joined" -> {
                endSession()
                peerIp = m.optString("ip", "?")
                val n = Crypto.randHex(16)
                pendingNonce = n
                sendText(JSONObject().put("type", "challenge").put("nonce", n).put("version", 3)
                    .put("name", cfg.deviceName)
                    .put("methods", org.json.JSONArray(listOf("password", "trust", "request"))))
            }
            "auth" -> {
                val hn = pendingNonce ?: return
                pendingNonce = null
                authenticate(m, hn, m.optString("vnonce", "").take(64))
            }
            "peer_left" -> {
                pendingNonce = null
                pendingReq?.let { cancelApproval() }
                pendingReq = null
                endSession()
            }
        }
    }

    private fun onBinary(b: ByteArray) {
        val ch = chan ?: return
        val (kind, body) = ch.decrypt(b) ?: return
        when (kind) {
            SecureChannel.MSG_JSON -> try { onSecure(JSONObject(String(body))) } catch (_: Exception) {}
            SecureChannel.MSG_FILE -> fileChunk(body)
        }
    }

    // ------------------------------------------------------------------ authentication
    private fun fail(code: String, count: Boolean, now: Long, locked: Boolean) {
        if (count && !locked) failures.add(now)
        sendText(JSONObject().put("type", "auth_fail").put("code", code))
    }

    private fun authenticate(m: JSONObject, hn: String, vn: String) {
        val now = System.currentTimeMillis()
        failures.removeAll { now - it > 300_000 }
        val locked = failures.size >= 5
        val method = m.optString("method", "password")
        val device = m.optString("device", "").take(64)
        val devName = m.optString("device_name", "").ifEmpty { "מכשיר לא ידוע" }.take(60)

        when (method) {
            "password" -> {
                if (locked) return fail("locked", false, now, locked)
                if (!cfg.hasPassword()) return fail("wrong_password", true, now, locked)
                val key = Crypto.unhex(cfg.key)
                if (!Crypto.equalsConstTime(Crypto.proof(key, "viewer", hn, vn), m.optString("proof")))
                    return fail("wrong_password", true, now, locked)
                success(key, hn, vn, m.optBoolean("remember") && device.isNotEmpty(), device, devName)
            }
            "trust" -> {
                if (locked) return fail("locked", false, now, locked)
                val e = cfg.trusted().optJSONObject(device) ?: return fail("not_trusted", false, now, locked)
                val key = Crypto.unhex(e.getString("key"))
                if (!Crypto.equalsConstTime(Crypto.proof(key, "viewer", hn, vn), m.optString("proof")))
                    return fail("not_trusted", true, now, locked)
                success(key, hn, vn, false, device, devName)
            }
            "request" -> {
                requests.removeAll { now - it > 120_000 }
                if (requests.size >= 3) return fail("too_many_requests", false, now, locked)
                requests.add(now)
                val kp = Crypto.ecdhKeyPair()
                val key = try {
                    Crypto.ecdhKey(kp, m.optString("pub"), hn, vn)
                } catch (e: Exception) {
                    return fail("protocol", false, now, locked)
                }
                sendText(JSONObject().put("type", "pending").put("pub", Crypto.hex(Crypto.encodePub(kp))))
                val req = PendingReq(Crypto.randHex(8), hn, vn, key, device, devName, peerIp,
                    Crypto.verificationCode(key))
                pendingReq = req
                main.post { showApproval(req) }
                main.postDelayed({ answer(req.id, null, false) }, 45_000)
            }
            else -> fail("protocol", false, now, locked)
        }
    }

    /** accept: true = approved, false = declined, null = timeout */
    fun answer(reqId: String, accept: Boolean?, always: Boolean) {
        exec.execute {
            val req = pendingReq
            if (req == null || req.id != reqId) return@execute
            pendingReq = null
            main.post { cancelApproval() }
            val now = System.currentTimeMillis()
            when (accept) {
                null -> fail("approval_timeout", false, now, false)
                false -> fail("declined", false, now, false)
                true -> success(req.key, req.hn, req.vn, always && req.device.isNotEmpty(), req.device, req.name)
            }
        }
    }

    private fun success(key: ByteArray, hn: String, vn: String, grant: Boolean, device: String, devName: String) {
        sendText(JSONObject().put("type", "auth_ok").put("proof", Crypto.proof(key, "host", hn, vn)))
        val w = ws
        val ch = SecureChannel({ bytes -> w?.send(bytes.toByteString()) ?: false }, Crypto.sessionKey(key, hn, vn))
        chan = ch
        if (grant) {
            val tk = Crypto.randBytes(32)
            val t = cfg.trusted()
            t.put(device, JSONObject().put("key", Crypto.hex(tk)).put("name", devName)
                .put("added", System.currentTimeMillis() / 1000))
            cfg.saveTrusted(t)
            ch.sendJson(JSONObject().put("type", "trust_grant").put("key", Crypto.hex(tk))
                .put("host_name", cfg.deviceName))
            main.post { notifyListener() }
        }
        startSession()
    }

    // ------------------------------------------------------------------ approval UI
    private fun showApproval(req: PendingReq) {
        val full = pi(Intent(this, ApproveActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), 20, activity = true)
        fun action(title: String, accept: Boolean, always: Boolean, rc: Int) =
            Notification.Action.Builder(Icon.createWithResource(this, R.drawable.ic_stat), title,
                pi(Intent(this, ActionReceiver::class.java).setAction(ActionReceiver.ACTION_ANSWER)
                    .putExtra("id", req.id).putExtra("accept", accept).putExtra("always", always), rc)).build()
        val n = Notification.Builder(this, CH_ALERT)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("🔔 בקשת גישה: ${req.name}")
            .setContentText("קוד אימות: ${req.code}")
            .setStyle(Notification.BigTextStyle().bigText(
                "«${req.name}» מבקש לשלוט בטלפון הזה.\nקוד אימות: ${req.code}\nודא שזה אותו קוד שמופיע אצל המבקש."))
            .setCategory(Notification.CATEGORY_CALL)
            .setFullScreenIntent(full, true)
            .setContentIntent(full)
            .setTimeoutAfter(45_000)
            .setAutoCancel(true)
            .addAction(action("✔ אשר", true, false, 21))
            .addAction(action("✔ אשר תמיד", true, true, 22))
            .addAction(action("✖ דחה", false, false, 23))
            .build()
        getSystemService(NotificationManager::class.java).notify(REQ_NOTIF_ID, n)
        try {
            startActivity(Intent(this, ApproveActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (_: Exception) { /* background start blocked - the notification is enough */ }
    }

    private fun cancelApproval() {
        getSystemService(NotificationManager::class.java).cancel(REQ_NOTIF_ID)
        ApproveActivity.current?.finish()
    }

    // ------------------------------------------------------------------ session
    private fun startSession() {
        sessionActive = true
        sessionIp = peerIp
        force = true
        synchronized(lock) { inFlight = 0 }
        main.post {
            getSystemService(NotificationManager::class.java).notify(NOTIF_ID, statusNotification())
            notifyListener()
        }
        val t = Thread({ streamLoop() }, "streamer")
        streamer = t
        t.start()
    }

    private fun endSession() {
        if (!sessionActive && chan == null) return
        sessionActive = false
        synchronized(lock) { lock.notifyAll() }
        streamer = null
        chan = null
        sessionIp = null
        fileAbort()
        main.post {
            ControlService.instance?.reset()
            getSystemService(NotificationManager::class.java).notify(NOTIF_ID, statusNotification())
            notifyListener()
        }
    }

    private fun kick() {
        chan?.sendJson(JSONObject().put("type", "bye").put("reason", "החיבור נותק על ידי המשתמש בטלפון"))
        endSession()
        ws?.cancel()
    }

    fun kickFromUi() = exec.execute { kick() }

    private fun onSecure(m: JSONObject) {
        if (!sessionActive) return
        when (m.optString("type")) {
            "ack" -> synchronized(lock) {
                inFlight = maxOf(0, inFlight - 1)
                lock.notifyAll()
            }
            "settings" -> {
                if (m.has("quality")) quality = m.optInt("quality", 60).coerceIn(10, 95)
                force = true
            }
            "file_begin" -> fileBegin(m.optString("name", "file"), m.optLong("size", 0))
            "file_end" -> fileEnd()
            else -> {
                val w = screenW
                val h = screenH
                main.post { ControlService.instance?.handle(m, w, h) }
            }
        }
    }

    // ------------------------------------------------------------------ screen capture
    private fun realSize(): Pair<Int, Int> {
        val wm = getSystemService(WindowManager::class.java)
        return if (Build.VERSION.SDK_INT >= 30) {
            val b = wm.maximumWindowMetrics.bounds
            Pair(b.width(), b.height())
        } else {
            val m = DisplayMetrics()
            @Suppress("DEPRECATION") wm.defaultDisplay.getRealMetrics(m)
            Pair(m.widthPixels, m.heightPixels)
        }
    }

    private fun startProjection(code: Int, data: Intent) {
        projection?.stop()
        val mpm = getSystemService(MediaProjectionManager::class.java)
        val p = mpm.getMediaProjection(code, data) ?: throw IllegalStateException("no projection")
        p.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                main.post {
                    projection = null
                    vdisplay?.release()
                    vdisplay = null
                    status("שיתוף המסך הופסק - פתח את האפליקציה כדי להפעיל שוב", online)
                }
            }
        }, main)
        projection = p
        vdisplay = null
        setupDisplay()
        status(if (online) "מוכן - ממתין לחיבורים" else statusText, online)
    }

    private fun setupDisplay() {
        val p = projection ?: return
        val (w, h) = realSize()
        screenW = w
        screenH = h
        val scale = minOf(1.0, 1280.0 / maxOf(w, h))
        val cw = ((w * scale).toInt() / 2) * 2
        val chh = ((h * scale).toInt() / 2) * 2
        val dpi = resources.displayMetrics.densityDpi
        synchronized(lock) {
            val old = reader
            val r = ImageReader.newInstance(cw, chh, PixelFormat.RGBA_8888, 2)
            r.setOnImageAvailableListener({ rr ->
                synchronized(lock) {
                    if (!sessionActive) {
                        try { rr.acquireLatestImage()?.close() } catch (_: Exception) {}
                    } else {
                        lock.notifyAll()
                    }
                }
            }, capHandler)
            reader = r
            val vd = vdisplay
            if (vd == null) {
                vdisplay = p.createVirtualDisplay("RemoteDesk", cw, chh, dpi,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, r.surface, null, null)
            } else {
                vd.resize(cw, chh, dpi)
                vd.surface = r.surface
            }
            old?.close()
        }
        force = true
    }

    /** Latest screen image as a bitmap, or null if nothing new. */
    private fun grab(): Bitmap? = synchronized(lock) {
        val r = reader ?: return null
        val im = try { r.acquireLatestImage() } catch (e: Exception) { null } ?: return null
        try {
            val plane = im.planes[0]
            val ps = plane.pixelStride
            val rowPad = plane.rowStride - ps * im.width
            val bmp = Bitmap.createBitmap(im.width + rowPad / ps, im.height, Bitmap.Config.ARGB_8888)
            bmp.copyPixelsFromBuffer(plane.buffer)
            if (rowPad == 0) bmp else Bitmap.createBitmap(bmp, 0, 0, im.width, im.height).also { bmp.recycle() }
        } finally {
            im.close()
        }
    }

    private fun placeholder(): Bitmap {
        val b = Bitmap.createBitmap(720, 1280, Bitmap.Config.ARGB_8888)
        val c = Canvas(b)
        c.drawColor(Color.rgb(29, 31, 35))
        val p = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = 40f; textAlign = Paint.Align.CENTER }
        c.drawText("שיתוף המסך בטלפון כבוי", 360f, 600f, p)
        p.textSize = 30f
        c.drawText("יש לפתוח את RemoteDesk בטלפון", 360f, 660f, p)
        c.drawText("וללחוץ \"הפעל שיתוף מסך\"", 360f, 705f, p)
        return b
    }

    private fun sendScreenInfo() {
        chan?.sendJson(JSONObject().put("type", "screen").put("width", screenW).put("height", screenH)
            .put("monitors", 1).put("monitor", 1))
    }

    private fun streamLoop() {
        var last: Bitmap? = null
        var lastSent = 0L
        var lastSizeCheck = 0L
        var size = Pair(0, 0)
        var noticeSent = false
        if (projection == null) main.post { askForProjection() }
        while (sessionActive) {
            val now = SystemClock.uptimeMillis()
            if (now - lastSizeCheck > 1000) {         // rotation / resolution change
                lastSizeCheck = now
                val s = realSize()
                if (s != size) {
                    size = s
                    if (projection != null) main.post { setupDisplay() } else { screenW = s.first; screenH = s.second }
                    sendScreenInfo()
                    force = true
                }
            }
            synchronized(lock) {
                if (inFlight >= 2) {
                    lock.wait(1000)
                    if (inFlight >= 2) inFlight = 0       // lost acks - don't stall forever
                }
            }
            if (!sessionActive) break
            var bmp: Bitmap? = null
            if (projection != null) {
                bmp = grab()
                noticeSent = false
            } else if (!noticeSent) {
                bmp = placeholder()
                noticeSent = true
            }
            if (bmp != null) {
                last?.recycle()
                last = bmp
            }
            if (bmp == null && !force) {
                synchronized(lock) { lock.wait(100) }
                continue
            }
            val frame = last ?: run { synchronized(lock) { lock.wait(100) }; null } ?: continue
            force = false
            val wait = 66 - (SystemClock.uptimeMillis() - lastSent)    // ~15 fps max
            if (wait > 0) Thread.sleep(wait)
            val bos = ByteArrayOutputStream()
            frame.compress(Bitmap.CompressFormat.JPEG, quality, bos)
            val ch = chan ?: break
            synchronized(lock) { inFlight++ }
            if (!ch.send(SecureChannel.MSG_FRAME, bos.toByteArray())) break
            lastSent = SystemClock.uptimeMillis()
        }
        last?.recycle()
    }

    private fun askForProjection() {
        val n = Notification.Builder(this, CH_ALERT)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("מישהו מחובר - יש להפעיל שיתוף מסך")
            .setContentText("לחץ כאן ואשר את שיתוף המסך")
            .setContentIntent(pi(Intent(this, MainActivity::class.java).putExtra("start_capture", true), 30, activity = true))
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java).notify(3, n)
    }

    // ------------------------------------------------------------------ files
    private fun fileBegin(name: String, size: Long) {
        fileAbort()
        val safe = name.replace(Regex("[\\\\/:*?\"<>|\\x00-\\x1f]"), "_").takeLast(150).ifEmpty { "file" }
        try {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, safe)
                put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/RemoteDesk")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("cannot create file")
            fileUri = uri
            fileOut = contentResolver.openOutputStream(uri)
            fileName = safe
            fileRemaining = size
        } catch (e: Exception) {
            chan?.sendJson(JSONObject().put("type", "file_error").put("message", e.message ?: "error"))
        }
    }

    private fun fileChunk(data: ByteArray) {
        val o = fileOut ?: return
        try {
            val n = minOf(data.size.toLong(), fileRemaining).toInt()
            o.write(data, 0, n)
            fileRemaining -= n
        } catch (e: Exception) {
            fileAbort()
        }
    }

    private fun fileEnd() {
        val o = fileOut ?: return
        val uri = fileUri ?: return
        try {
            o.close()
            contentResolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
            chan?.sendJson(JSONObject().put("type", "file_done").put("path", "Download/RemoteDesk/$fileName"))
        } catch (e: Exception) {
            chan?.sendJson(JSONObject().put("type", "file_error").put("message", e.message ?: "error"))
        }
        fileOut = null
        fileUri = null
    }

    private fun fileAbort() {
        try { fileOut?.close() } catch (_: Exception) {}
        fileUri?.let { try { contentResolver.delete(it, null, null) } catch (_: Exception) {} }
        fileOut = null
        fileUri = null
    }
}
