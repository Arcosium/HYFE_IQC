package uk.aive.genomicwqb

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** 재부팅 후에도 바이오 인증 감시를 이어간다 — 앱을 다시 열지 않아도 알림이 온다. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            PersonaWatchService.start(context)
        }
    }
}
