package com.remotedesk.host

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.LinearLayout
import android.widget.TextView

/** "Someone asks to control this phone" screen (also shown over the lock screen). */
class ApproveActivity : Activity() {

    companion object {
        @Volatile var current: ApproveActivity? = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val req = HostService.instance?.pendingReq
        if (req == null) {
            finish()
            return
        }
        current = this
        setFinishOnTouchOutside(false)
        val d = resources.displayMetrics.density
        fun px(v: Int) = (v * d).toInt()

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutDirection = View.LAYOUT_DIRECTION_RTL
            setPadding(px(22), px(20), px(22), px(16))
            setBackgroundColor(Color.WHITE)
        }
        fun text(s: String, size: Float, color: Int = Color.rgb(29, 31, 35), bold: Boolean = false) =
            TextView(this).apply {
                text = s
                textSize = size
                setTextColor(color)
                gravity = Gravity.START
                textDirection = View.TEXT_DIRECTION_RTL
                if (bold) setTypeface(typeface, Typeface.BOLD)
                setPadding(0, px(4), 0, px(4))
            }

        root.addView(text("🔔 בקשת גישה לטלפון", 22f, Color.rgb(198, 40, 40), true))
        root.addView(text("«${req.name}» מבקש לשלוט בטלפון הזה.", 17f))
        root.addView(text("כתובת: ${req.ip}", 13f, Color.GRAY))
        root.addView(text("קוד אימות - ודא שזה אותו קוד שמופיע אצל המבקש:", 15f).apply { setPadding(0, px(14), 0, 0) })
        root.addView(TextView(this).apply {
            text = req.code
            textSize = 38f
            typeface = Typeface.MONOSPACE
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(Color.rgb(198, 40, 40))
            gravity = Gravity.CENTER
            textDirection = View.TEXT_DIRECTION_LTR
        })
        val always = CheckBox(this).apply {
            text = "לאפשר למכשיר הזה להיכנס תמיד, בלי לשאול שוב"
            textSize = 15f
        }
        root.addView(always)

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, px(14), 0, 0)
        }
        fun button(label: String, bg: Int, fg: Int, onClick: () -> Unit) = Button(this).apply {
            text = label
            textSize = 17f
            setTextColor(fg)
            setBackgroundColor(bg)
            layoutParams = LinearLayout.LayoutParams(0, px(52), 1f).apply { setMargins(px(4), 0, px(4), 0) }
            setOnClickListener { onClick() }
        }
        row.addView(button("✔ אשר", Color.rgb(10, 125, 0), Color.WHITE) {
            HostService.instance?.answer(req.id, true, always.isChecked)
            finish()
        })
        row.addView(button("✖ דחה", Color.rgb(235, 235, 235), Color.BLACK) {
            HostService.instance?.answer(req.id, false, false)
            finish()
        })
        root.addView(row)
        setContentView(root)
    }

    override fun onDestroy() {
        if (current === this) current = null
        super.onDestroy()
    }
}
