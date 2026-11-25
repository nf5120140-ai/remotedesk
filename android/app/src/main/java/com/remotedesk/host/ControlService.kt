package com.remotedesk.host

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.PointF
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONObject
import kotlin.math.hypot

/**
 * Accessibility service that turns remote mouse/keyboard events into touches:
 *  left button  -> tap / long-press / drag (performed on release)
 *  right button -> Back, middle button -> Recent apps
 *  wheel        -> swipe, keyboard -> text in the focused field
 */
class ControlService : AccessibilityService() {

    companion object {
        @Volatile var instance: ControlService? = null
    }

    private var x = 0f
    private var y = 0f
    private var down = false
    private var downAt = 0L
    private val path = ArrayList<PointF>()
    private var ctrl = false

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        HostService.notifyListener()
    }

    override fun onDestroy() {
        instance = null
        HostService.notifyListener()
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    fun reset() {
        down = false
        path.clear()
        ctrl = false
    }

    fun handle(m: JSONObject, w: Int, h: Int) {
        when (m.optString("type")) {
            "move" -> {
                x = (m.optDouble("x", 0.0).coerceIn(0.0, 1.0) * (w - 1)).toFloat()
                y = (m.optDouble("y", 0.0).coerceIn(0.0, 1.0) * (h - 1)).toFloat()
                if (down) {
                    val l = path.lastOrNull()
                    if (l == null || hypot(l.x - x, l.y - y) > 3f) path.add(PointF(x, y))
                }
            }
            "button" -> {
                val pressed = m.optBoolean("down")
                when (m.optString("b")) {
                    "left" -> if (pressed) {
                        down = true
                        downAt = SystemClock.uptimeMillis()
                        path.clear()
                        path.add(PointF(x, y))
                    } else if (down) {
                        down = false
                        gesture(ArrayList(path), SystemClock.uptimeMillis() - downAt)
                    }
                    "right" -> if (!pressed) performGlobalAction(GLOBAL_ACTION_BACK)
                    "middle" -> if (!pressed) performGlobalAction(GLOBAL_ACTION_RECENTS)
                }
            }
            "scroll" -> {
                // wheel up (dy>0) = finger moves down
                val dy = m.optInt("dy", 0)
                val to = (y + dy * h * 0.18f).coerceIn(1f, h - 2f)
                gesture(arrayListOf(PointF(x, y), PointF(x, to)), 220)
            }
            "key" -> key(m.optString("name"), m.optString("char"), m.optBoolean("down"))
            "type_text" -> insertText(m.optString("text"))
            "cad" -> performGlobalAction(GLOBAL_ACTION_RECENTS)
        }
    }

    private fun gesture(points: List<PointF>, elapsed: Long) {
        if (points.isEmpty()) return
        val first = points[0]
        val moved = points.any { hypot(it.x - first.x, it.y - first.y) > 15f }
        val p = Path()
        p.moveTo(first.x, first.y)
        val duration: Long
        if (!moved) {
            duration = if (elapsed < 400) 40 else elapsed.coerceAtMost(5000)     // tap or long-press
        } else {
            for (pt in points.drop(1)) p.lineTo(pt.x, pt.y)
            duration = elapsed.coerceIn(80, 5000)
        }
        try {
            val g = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(p, 0, duration))
                .build()
            dispatchGesture(g, null, null)
        } catch (_: Exception) {}
    }

    // ------------------------------------------------------------------ keyboard
    private fun key(name: String, char: String, pressed: Boolean) {
        if (name == "Control_L" || name == "Control_R") {
            ctrl = pressed
            return
        }
        if (!pressed) return
        when (name) {
            "Escape" -> performGlobalAction(GLOBAL_ACTION_BACK)
            "Super_L", "Super_R", "Win_L", "Win_R", "Meta_L", "Meta_R" -> performGlobalAction(GLOBAL_ACTION_HOME)
            "BackSpace" -> edit { t, s, e -> if (s != e) Pair(t.removeRange(s, e), s) else if (s > 0) Pair(t.removeRange(s - 1, s), s - 1) else null }
            "Delete" -> edit { t, s, e -> if (s != e) Pair(t.removeRange(s, e), s) else if (s < t.length) Pair(t.removeRange(s, s + 1), s) else null }
            "Return", "KP_Enter" -> enter()
            "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Caps_Lock", "Num_Lock", "Tab" -> {}
            else -> {
                if (ctrl) {
                    val n = focused() ?: return
                    when (name.lowercase()) {
                        "c" -> n.performAction(AccessibilityNodeInfo.ACTION_COPY)
                        "v" -> n.performAction(AccessibilityNodeInfo.ACTION_PASTE)
                        "x" -> n.performAction(AccessibilityNodeInfo.ACTION_CUT)
                        "a" -> select(n, 0, currentText(n).length)
                    }
                } else if (char.isNotEmpty()) {
                    insertText(char)
                } else if (name == "space") {
                    insertText(" ")
                } else if (name.length == 1) {
                    insertText(name)
                }
            }
        }
    }

    private fun focused(): AccessibilityNodeInfo? =
        try { rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) } catch (_: Exception) { null }

    private fun currentText(n: AccessibilityNodeInfo): String =
        if (n.isShowingHintText) "" else (n.text?.toString() ?: "")

    private fun select(n: AccessibilityNodeInfo, s: Int, e: Int) {
        val b = Bundle()
        b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, s)
        b.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, e)
        n.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, b)
    }

    /** f(text, selStart, selEnd) -> (newText, cursor) or null */
    private fun edit(f: (String, Int, Int) -> Pair<String, Int>?) {
        val n = focused() ?: return
        if (!n.isEditable) return
        val t = currentText(n)
        var s = n.textSelectionStart
        var e = n.textSelectionEnd
        if (s < 0 || s > t.length) s = t.length
        if (e < s || e > t.length) e = s
        val r = f(t, s, e) ?: return
        val b = Bundle()
        b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, r.first)
        n.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, b)
        select(n, r.second, r.second)
    }

    fun insertText(ins: String) {
        if (ins.isEmpty()) return
        edit { t, s, e -> Pair(t.substring(0, s) + ins + t.substring(e), s + ins.length) }
    }

    private fun enter() {
        val n = focused()
        if (n != null && Build.VERSION.SDK_INT >= 30) {
            if (n.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)) return
        }
        insertText("\n")
    }
}
