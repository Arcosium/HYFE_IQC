package uk.aive.genomicwqb

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.IBinder
import android.util.Log
import android.webkit.CookieManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * WQB 바이오 인증(Persona) 감시 — 포그라운드 서비스.
 *
 * 서버의 `/api/account/wqb-persona-watch` 를 [POLL_INTERVAL_MS] 마다 호출한다. 이 엔드포인트는
 * 로컬 `.pending` 파일만 읽는 **완전 수동** 경로라 WQB 로 나가는 호출이 없다.
 * (`/wqb-persona-status` 를 폴링하면 POST /authentication 이 반복돼 WQB 가
 *  BIOMETRICS_THROTTLED 를 영구 재무장한다 — 절대 그쪽을 폴링하지 말 것.)
 *
 * ⚠ 알림에 Persona 인증 URL 을 실어 나르지 않는다. 그 링크는 발급하는 순간 직전 Persona
 * 세션을 무효화하는 **일회성** 링크라서, 알림에 박아 두면 사용자가 탭할 때쯤엔 이미 죽어 있다
 * (열리자마자 무한 새로고침 → 'session expired', 사장 보고 2026-07-10). 알림은 앱을 열기만
 * 하고, 링크는 사용자가 인증 배너를 누르는 그 순간 서버가 `/wqb-persona-link` 로 발급한다.
 *
 * 알림은 challenge(inquiry) 당 한 번만 띄우고, 인증이 끝나 pending 이 사라지면 스스로 지운다
 * (그래서 [setAutoCancel] 을 켜지 않는다 — 탭했다고 사라지면 되돌아올 입구가 없어진다).
 *
 * 세션은 WebView 로그인으로 CookieManager 에 저장된 `hyfe_session` 쿠키를 그대로 쓴다.
 * 쿠키가 없으면(미로그인) 조용히 건너뛴다.
 */
class PersonaWatchService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var loop: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannels()
        startForeground(ONGOING_NOTIF_ID, buildOngoingNotification())
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (loop?.isActive != true) {
            loop = scope.launch {
                while (isActive) {
                    try {
                        poll()
                    } catch (e: Exception) {
                        Log.w(TAG, "poll failed: ${e.message}")
                    }
                    delay(POLL_INTERVAL_MS)
                }
            }
        }
        return START_STICKY   // 시스템이 죽여도 다시 살아나 감시를 이어간다
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    // ─── 폴링 ────────────────────────────────────────────────

    private fun poll() {
        val cookie = CookieManager.getInstance().getCookie(BASE_URL)
        if (cookie.isNullOrBlank()) {
            Log.i(TAG, "세션 쿠키 없음 — 아직 로그인 전")
            return
        }

        val conn = URL("$BASE_URL$WATCH_ENDPOINT").openConnection() as HttpURLConnection
        val body = try {
            conn.connectTimeout = 10_000
            conn.readTimeout = 10_000
            conn.requestMethod = "GET"
            conn.setRequestProperty("Accept", "application/json")
            conn.setRequestProperty("User-Agent", "GenomicWQB-Watch/1.0")
            conn.setRequestProperty("Cookie", cookie)
            when (val code = conn.responseCode) {
                200 -> conn.inputStream.bufferedReader().use { it.readText() }
                401 -> { Log.w(TAG, "401 — 세션 만료"); null }
                else -> { Log.w(TAG, "예상 밖 응답 $code"); null }
            }
        } finally {
            conn.disconnect()
        } ?: return

        val json = JSONObject(body)
        val prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        if (!json.optBoolean("persona_required", false)) {
            // 인증이 끝났거나 대기 중인 challenge 가 없다 → 기준선을 지워 다음 건은 다시 알린다.
            if (prefs.contains(KEY_NOTIFIED_INQUIRY)) {
                prefs.edit().remove(KEY_NOTIFIED_INQUIRY).apply()
                NotificationManagerCompat.from(this).cancel(ALERT_NOTIF_ID)
            }
            return
        }

        val inquiry = json.optString("inquiry", "").ifBlank { "pending" }
        if (prefs.getString(KEY_NOTIFIED_INQUIRY, null) == inquiry) return   // 같은 challenge — 재알림 안 함

        notifyPersonaRequired()
        prefs.edit().putString(KEY_NOTIFIED_INQUIRY, inquiry).apply()
    }

    // ─── 알림 ────────────────────────────────────────────────

    private fun createChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = getSystemService(NotificationManager::class.java)
        if (mgr.getNotificationChannel(CHANNEL_ONGOING) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL_ONGOING, "인증 감시", NotificationManager.IMPORTANCE_MIN)
                    .apply { description = "WQB 바이오 인증 상태를 백그라운드에서 확인합니다" }
            )
        }
        if (mgr.getNotificationChannel(CHANNEL_ALERT) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL_ALERT, "바이오 인증 필요", NotificationManager.IMPORTANCE_HIGH)
                    .apply { description = "WorldQuant Brain 바이오 인증이 필요할 때 알립니다" }
            )
        }
    }

    private fun buildOngoingNotification(): Notification =
        NotificationCompat.Builder(this, CHANNEL_ONGOING)
            .setSmallIcon(R.drawable.ic_notif)
            .setContentTitle("GenomicWQB")
            .setContentText("바이오 인증 상태 감시 중")
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setContentIntent(
                PendingIntent.getActivity(
                    this, 0, Intent(this, MainActivity::class.java),
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
                )
            )
            .build()

    private fun notifyPersonaRequired() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
            if (!granted) return
        }

        // ⚠ 앱을 열 뿐, Persona URL 을 직접 열지 않는다. 그 링크는 일회성이라 알림에 박아 두면
        //    탭할 때쯤 죽어 있다. 앱 안 인증 배너를 누르면 서버가 그 시점에 새 링크를 발급하고,
        //    WebView 가 shouldOverrideUrlLoading 으로 시스템 브라우저(카메라 필요)에 넘긴다.
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pi = PendingIntent.getActivity(
            this, 1, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val text = "GenomicWQB 를 열어 '여기서 인증 완료하기' 를 누르세요"

        val notif = NotificationCompat.Builder(this, CHANNEL_ALERT)
            .setSmallIcon(R.drawable.ic_notif)
            .setContentTitle("WQB 바이오 인증 필요")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setAutoCancel(false)   // 인증이 끝나 pending 이 사라질 때 poll() 이 지운다
            .setContentIntent(pi)
            .build()

        NotificationManagerCompat.from(this).notify(ALERT_NOTIF_ID, notif)
    }

    companion object {
        private const val TAG = "PersonaWatch"
        private const val BASE_URL = "https://iqc.ai-ve.uk"
        private const val WATCH_ENDPOINT = "/api/account/wqb-persona-watch"
        private const val POLL_INTERVAL_MS = 60_000L
        private const val PREFS = "genomicwqb_prefs"
        private const val KEY_NOTIFIED_INQUIRY = "notified_inquiry"
        private const val CHANNEL_ONGOING = "persona_watch_ongoing"
        private const val CHANNEL_ALERT = "persona_required"
        private const val ONGOING_NOTIF_ID = 1001
        private const val ALERT_NOTIF_ID = 1002

        fun start(context: Context) {
            val intent = Intent(context, PersonaWatchService::class.java)
            ContextCompat.startForegroundService(context, intent)
        }
    }
}
