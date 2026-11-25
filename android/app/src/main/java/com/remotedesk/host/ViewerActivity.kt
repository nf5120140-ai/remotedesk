package com.remotedesk.host

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * The app's start screen: control other computers from inside the app
 * (saved computers, connect by number, full-screen remote screen).
 * The UI is the same one the relay serves, so it updates without reinstalling.
 */
class ViewerActivity : Activity() {

    private lateinit var web: WebView
    private lateinit var cfg: Config
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val REQ_FILE = 7
    @Volatile private var inSession = false

    private fun baseUrl(): String =
        cfg.server.replace("wss://", "https://").replace("ws://", "http://").trimEnd('/') + "/"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cfg = Config(this)
        window.statusBarColor = Color.rgb(18, 19, 22)
        web = WebView(this)
        web.setBackgroundColor(Color.rgb(18, 19, 22))
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            mediaPlaybackRequiresUserGesture = true
            setSupportZoom(false)
            userAgentString = "$userAgentString RemoteDeskApp/1"
        }
        web.addJavascriptInterface(Bridge(), "RemoteDeskApp")
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                if (u.host == Uri.parse(baseUrl()).host) return false
                try { startActivity(Intent(Intent.ACTION_VIEW, u)) } catch (_: Exception) {}
                return true
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (!request.isForMainFrame) return
                view.loadDataWithBaseURL(null, offlinePage(), "text/html", "utf-8", null)
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>,
                                           params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try {
                    @Suppress("DEPRECATION")
                    startActivityForResult(params.createIntent(), REQ_FILE)
                    true
                } catch (e: Exception) {
                    fileCallback = null
                    false
                }
            }
        }
        setContentView(web)
        if (savedInstanceState != null) web.restoreState(savedInstanceState) else web.loadUrl(baseUrl())
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    private fun offlinePage(): String = """
        <html dir="rtl"><body style="background:#121316;color:#eceef1;font-family:sans-serif;display:flex;
        align-items:center;justify-content:center;height:100vh;margin:0;text-align:center;padding:20px">
        <div><h2>אין חיבור לשרת</h2><p style="color:#9aa0a8">בדוק את החיבור לאינטרנט.<br>אם השרת היה כבוי, הוא מתעורר תוך כחצי דקה.</p>
        <button onclick="location.href='${baseUrl()}'" style="margin-top:16px;padding:14px 28px;font-size:18px;
        border:0;border-radius:10px;background:#c62828;color:#fff">נסה שוב</button></div></body></html>
    """.trimIndent()

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_FILE) {
            fileCallback?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data))
            fileCallback = null
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        // let the page decide (disconnect question / cancel waiting); otherwise leave the app
        web.evaluateJavascript("(window.appBack && window.appBack()) ? 'yes' : 'no'") { r ->
            if (r?.contains("yes") != true) {
                @Suppress("DEPRECATION")
                super.onBackPressed()
            }
        }
    }

    private fun setImmersive(on: Boolean) {
        if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (Build.VERSION.SDK_INT >= 30) {
            val c = window.insetsController ?: return
            if (on) {
                c.hide(WindowInsets.Type.statusBars())
                c.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            } else {
                c.show(WindowInsets.Type.statusBars())
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = if (on)
                View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY else 0
        }
    }

    /** Called from the page (window.RemoteDeskApp). */
    inner class Bridge {
        @JavascriptInterface
        fun openPhoneSettings() {
            runOnUiThread { startActivity(Intent(this@ViewerActivity, MainActivity::class.java)) }
        }

        @JavascriptInterface
        fun setInSession(active: Boolean) {
            inSession = active
            runOnUiThread { setImmersive(active) }
        }

        @JavascriptInterface
        fun phoneId(): String = Config.formatId(cfg.id)

        @JavascriptInterface
        fun phoneStatus(): String {
            val svc = HostService.instance
            return when {
                svc?.hasProjection == true && HostService.online -> "ready"
                svc != null -> "partial"
                else -> "off"
            }
        }
    }
}
