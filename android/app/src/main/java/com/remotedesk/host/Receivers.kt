package com.remotedesk.host

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Buttons in notifications: approve / decline requests, disconnect. */
class ActionReceiver : BroadcastReceiver() {
    companion object {
        const val ACTION_ANSWER = "com.remotedesk.host.ANSWER"
    }

    override fun onReceive(context: Context, intent: Intent) {
        val svc = HostService.instance ?: return
        when (intent.action) {
            ACTION_ANSWER -> svc.answer(
                intent.getStringExtra("id") ?: return,
                intent.getBooleanExtra("accept", false),
                intent.getBooleanExtra("always", false))
            HostService.ACTION_KICK -> svc.kickFromUi()
        }
    }
}

/**
 * Android does not allow starting screen capture without the user's consent,
 * so after a reboot we only remind the user to open the app with one tap.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val nm = context.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(HostService.CH_STATUS, "מצב RemoteDesk", NotificationManager.IMPORTANCE_LOW))
        val pi = PendingIntent.getActivity(context, 40,
            Intent(context, MainActivity::class.java).putExtra("start_capture", true),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        nm.notify(4, Notification.Builder(context, HostService.CH_STATUS)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("RemoteDesk כבוי")
            .setContentText("לחץ כדי לאפשר גישה מרחוק לטלפון")
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build())
    }
}
