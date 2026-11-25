package com.remotedesk.host

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : Activity() {

    private lateinit var cfg: Config
    private lateinit var idText: TextView
    private lateinit var statusText: TextView
    private lateinit var sessionBox: LinearLayout
    private lateinit var sessionText: TextView
    private lateinit var captureBtn: Button
    private lateinit var captureState: TextView
    private lateinit var a11yState: TextView
    private lateinit var a11yBtn: Button
    private lateinit var pwState: TextView
    private lateinit var trustedBox: LinearLayout
    private lateinit var batteryBtn: Button
    private val red = Color.rgb(198, 40, 40)
    private val green = Color.rgb(10, 125, 0)
    private val dark = Color.rgb(29, 31, 35)

    private val REQ_CAPTURE = 1
    private fun px(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cfg = Config(this)
        window.statusBarColor = red
        buildUi()
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2)
        }
        if (intent?.getBooleanExtra("start_capture", false) == true) requestCapture()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.getBooleanExtra("start_capture", false)) requestCapture()
    }

    override fun onResume() {
        super.onResume()
        HostService.listener = { runOnUiThread { refresh() } }
        refresh()
    }

    override fun onPause() {
        HostService.listener = null
        super.onPause()
    }

    // ------------------------------------------------------------------ UI building
    private fun card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(px(16), px(14), px(16), px(14))
        background = GradientDrawable().apply {
            setColor(Color.WHITE)
            cornerRadius = px(14).toFloat()
            setStroke(px(1), Color.rgb(226, 228, 232))
        }
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT).apply { setMargins(0, px(12), 0, 0) }
    }

    private fun text(s: String, size: Float = 15f, color: Int = dark, bold: Boolean = false) = TextView(this).apply {
        text = s
        textSize = size
        setTextColor(color)
        textDirection = View.TEXT_DIRECTION_RTL
        if (bold) setTypeface(typeface, Typeface.BOLD)
        setPadding(0, px(3), 0, px(3))
    }

    private fun button(s: String, primary: Boolean = false, onClick: () -> Unit) = Button(this).apply {
        text = s
        isAllCaps = false
        textSize = 15f
        setTextColor(if (primary) Color.WHITE else dark)
        background = GradientDrawable().apply {
            setColor(if (primary) red else Color.rgb(240, 241, 243))
            cornerRadius = px(10).toFloat()
        }
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, px(48)).apply {
            setMargins(0, px(8), 0, 0)
        }
        setOnClickListener { onClick() }
    }

    private fun buildUi() {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutDirection = View.LAYOUT_DIRECTION_RTL
            setPadding(px(16), px(16), px(16), px(32))
            setBackgroundColor(Color.rgb(244, 245, 247))
        }

        root.addView(text("RemoteDesk", 26f, red, true).apply { gravity = Gravity.CENTER_HORIZONTAL })
        root.addView(text("שליטה מרחוק בטלפון הזה", 14f, Color.GRAY).apply { gravity = Gravity.CENTER_HORIZONTAL })

        // ---- ID + status
        val c1 = card()
        c1.addView(text("המספר של הטלפון:", 14f, Color.GRAY))
        idText = TextView(this).apply {
            textSize = 34f
            setTextColor(red)
            setTypeface(Typeface.MONOSPACE, Typeface.BOLD)
            gravity = Gravity.CENTER
            textDirection = View.TEXT_DIRECTION_LTR
        }
        c1.addView(idText)
        c1.addView(button("העתק מספר") {
            getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("id", cfg.id))
            Toast.makeText(this, "המספר הועתק", Toast.LENGTH_SHORT).show()
        })
        statusText = text("", 14f, Color.GRAY)
        c1.addView(statusText)
        root.addView(c1)

        // ---- session banner
        sessionBox = card().apply {
            background = GradientDrawable().apply { setColor(Color.rgb(255, 235, 235)); cornerRadius = px(14).toFloat() }
        }
        sessionText = text("", 16f, red, true)
        sessionBox.addView(sessionText)
        sessionBox.addView(button("נתק עכשיו", true) { HostService.instance?.kickFromUi() })
        root.addView(sessionBox)

        // ---- step 1: screen sharing
        val c2 = card()
        c2.addView(text("1. שיתוף מסך", 17f, dark, true))
        captureState = text("", 14f)
        c2.addView(captureState)
        captureBtn = button("הפעל שיתוף מסך", true) {
            if (HostService.instance?.hasProjection == true) {
                startService(Intent(this, HostService::class.java).setAction(HostService.ACTION_STOP))
                refresh()
            } else requestCapture()
        }
        c2.addView(captureBtn)
        root.addView(c2)

        // ---- step 2: touch control
        val c3 = card()
        c3.addView(text("2. שליטה במגע ובמקלדת", 17f, dark, true))
        a11yState = text("", 14f)
        c3.addView(a11yState)
        c3.addView(text("בהגדרות: נגישות ← אפליקציות שהורדו ← RemoteDesk ← הפעל.", 13f, Color.GRAY))
        a11yBtn = button("פתח הגדרות נגישות") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        c3.addView(a11yBtn)
        root.addView(c3)

        // ---- step 3: password (optional)
        val c4 = card()
        c4.addView(text("3. סיסמה (לא חובה)", 17f, dark, true))
        pwState = text("", 14f)
        c4.addView(pwState)
        c4.addView(text("בלי סיסמה אפשר להתחבר רק באישור כאן בטלפון. עם סיסמה אפשר להתחבר גם כשאף אחד לא ליד הטלפון.", 13f, Color.GRAY))
        val pw = EditText(this).apply {
            hint = "סיסמה חדשה (8 תווים לפחות)"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            textDirection = View.TEXT_DIRECTION_LTR
        }
        c4.addView(pw)
        c4.addView(button("שמור סיסמה") {
            val v = pw.text.toString()
            if (v.length < 8) {
                Toast.makeText(this, "8 תווים לפחות", Toast.LENGTH_SHORT).show()
                return@button
            }
            Thread {
                cfg.setPassword(v)
                runOnUiThread {
                    pw.setText("")
                    Toast.makeText(this, "הסיסמה נשמרה", Toast.LENGTH_SHORT).show()
                    HostService.instance?.reconnect()
                    refresh()
                }
            }.start()
        })
        root.addView(c4)

        // ---- trusted devices
        val c5 = card()
        c5.addView(text("מורשים להיכנס לטלפון הזה", 17f, dark, true))
        trustedBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        c5.addView(trustedBox)
        root.addView(c5)

        // ---- control other computer
        val c6 = card()
        c6.addView(text("שליטה במחשב אחר מהטלפון", 17f, dark, true))
        c6.addView(button("פתח את מסך השליטה") {
            val url = cfg.server.replace("wss://", "https://").replace("ws://", "http://")
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        })
        root.addView(c6)

        // ---- settings
        val c7 = card()
        c7.addView(text("הגדרות", 17f, dark, true))
        batteryBtn = button("אפשר פעולה ברקע (חיסכון בסוללה)") {
            @Suppress("BatteryLife")
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        }
        c7.addView(batteryBtn)
        c7.addView(text("שם הטלפון (מוצג לצד השני):", 14f, Color.GRAY))
        val name = EditText(this).apply { setText(cfg.deviceName) }
        c7.addView(name)
        c7.addView(text("כתובת שרת:", 14f, Color.GRAY))
        val server = EditText(this).apply { setText(cfg.server); textDirection = View.TEXT_DIRECTION_LTR; textSize = 13f }
        c7.addView(server)
        c7.addView(button("שמור הגדרות") {
            cfg.deviceName = name.text.toString().trim().ifEmpty { cfg.deviceName }
            cfg.server = server.text.toString().trim().ifEmpty { Config.DEFAULT_SERVER }
            HostService.instance?.reconnect()
            Toast.makeText(this, "נשמר", Toast.LENGTH_SHORT).show()
        })
        root.addView(c7)

        root.addView(text("גרסה ${packageManager.getPackageInfo(packageName, 0).versionName}", 12f, Color.GRAY)
            .apply { gravity = Gravity.CENTER_HORIZONTAL; setPadding(0, px(14), 0, 0) })

        setContentView(ScrollView(this).apply { addView(root) })
    }

    // ------------------------------------------------------------------ state
    private fun a11yEnabled(): Boolean {
        if (ControlService.instance != null) return true
        val s = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        return s.contains("$packageName/")
    }

    private fun refresh() {
        idText.text = Config.formatId(cfg.id)
        val svc = HostService.instance
        statusText.text = (if (HostService.online) "● " else "○ ") + HostService.statusText
        statusText.setTextColor(if (HostService.online) green else Color.GRAY)

        val ip = HostService.sessionIp
        sessionBox.visibility = if (ip != null) View.VISIBLE else View.GONE
        sessionText.text = "⚠ הטלפון נשלט כרגע מרחוק ($ip)"

        val capturing = svc?.hasProjection == true
        captureState.text = if (capturing) "✅ פעיל - אפשר להתחבר לטלפון" else "❌ כבוי - לחץ והפעל (צריך לאשר פעם אחת אחרי כל הפעלה מחדש של הטלפון)"
        captureState.setTextColor(if (capturing) green else red)
        captureBtn.text = if (capturing) "עצור גישה מרחוק" else "הפעל שיתוף מסך"

        val a11y = a11yEnabled()
        a11yState.text = if (a11y) "✅ פעיל - הצד השני יכול לגעת ולהקליד" else "❌ כבוי - הצד השני יוכל רק לראות"
        a11yState.setTextColor(if (a11y) green else red)
        a11yBtn.visibility = if (a11y) View.GONE else View.VISIBLE

        pwState.text = if (cfg.hasPassword()) "✅ מוגדרת סיסמה" else "אין סיסמה"

        val pm = getSystemService(PowerManager::class.java)
        batteryBtn.visibility = if (pm.isIgnoringBatteryOptimizations(packageName)) View.GONE else View.VISIBLE

        trustedBox.removeAllViews()
        val t = cfg.trusted()
        val keys = t.keys().asSequence().toList()
        if (keys.isEmpty()) {
            trustedBox.addView(text("אין עדיין. מכשיר נוסף כאן כשמאשרים בקשה עם \"אשר תמיד\".", 13f, Color.GRAY))
        }
        val fmt = SimpleDateFormat("dd/MM/yyyy", Locale.getDefault())
        for (k in keys) {
            val e = t.optJSONObject(k) ?: continue
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(0, px(6), 0, px(6))
            }
            row.addView(text("${e.optString("name")}  ·  ${fmt.format(Date(e.optLong("added") * 1000))}", 15f).apply {
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            })
            row.addView(Button(this).apply {
                text = "בטל"
                isAllCaps = false
                setOnClickListener {
                    AlertDialog.Builder(this@MainActivity)
                        .setMessage("לבטל את ההרשאה של «${e.optString("name")}»?")
                        .setPositiveButton("בטל הרשאה") { _, _ ->
                            val all = cfg.trusted()
                            all.remove(k)
                            cfg.saveTrusted(all)
                            refresh()
                        }
                        .setNegativeButton("השאר", null)
                        .show()
                }
            })
            trustedBox.addView(row)
        }
    }

    // ------------------------------------------------------------------ screen capture consent
    private fun requestCapture() {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        @Suppress("DEPRECATION")
        startActivityForResult(mpm.createScreenCaptureIntent(), REQ_CAPTURE)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_CAPTURE) return
        if (resultCode != RESULT_OK || data == null) {
            Toast.makeText(this, "שיתוף המסך לא אושר", Toast.LENGTH_SHORT).show()
            return
        }
        startForegroundService(Intent(this, HostService::class.java)
            .putExtra(HostService.EXTRA_CODE, resultCode)
            .putExtra(HostService.EXTRA_DATA, data))
        if (!a11yEnabled()) {
            AlertDialog.Builder(this)
                .setTitle("עוד צעד אחד")
                .setMessage("כדי שהצד השני יוכל גם לגעת ולהקליד (ולא רק לראות), צריך להפעיל את RemoteDesk בהגדרות הנגישות.")
                .setPositiveButton("פתח הגדרות") { _, _ -> startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
                .setNegativeButton("אחר כך", null)
                .show()
        }
        window.decorView.postDelayed({ refresh() }, 800)
    }
}
