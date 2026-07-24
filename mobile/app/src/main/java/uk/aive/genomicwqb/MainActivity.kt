package uk.aive.genomicwqb

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import uk.aive.genomicwqb.databinding.ActivityMainBinding

/** iqc.ai-ve.uk 웹앱을 감싸는 WebView 셸. 로그인 세션은 CookieManager 에 남아 감시 서비스가 재사용한다. */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var isPageLoaded = false

    private var lastBackPressTime: Long = 0L
    private var backPressToast: Toast? = null

    companion object {
        private const val WEB_URL = "https://iqc.ai-ve.uk"
        private const val HOST = "iqc.ai-ve.uk"
        private const val KEY_URL = "current_url"
        private const val BACK_EXIT_WINDOW_MS = 2000L
    }

    // 알림 권한을 받은 뒤에 감시 서비스를 띄운다 — 거부되면 서비스는 돌지만 알림이 안 뜬다.
    private val notifPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { PersonaWatchService.start(applicationContext) }

    override fun onCreate(savedInstanceState: Bundle?) {
        val splashScreen = installSplashScreen()
        splashScreen.setKeepOnScreenCondition { !isPageLoaded }

        super.onCreate(savedInstanceState)
        setupEdgeToEdge()

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        applySystemBarInsets()

        setupWebView()
        binding.swipeRefresh.isEnabled = false
        setupBackNavigation()
        binding.btnRetry.setOnClickListener {
            if (isNetworkAvailable()) { hideErrorState(); loadUrl(WEB_URL) }
        }

        requestNotificationPermissionThenWatch()

        val urlToLoad = savedInstanceState?.getString(KEY_URL) ?: WEB_URL
        if (isNetworkAvailable()) loadUrl(urlToLoad) else showErrorState()
    }

    /** POST_NOTIFICATIONS 를 먼저 묻고, 결과와 무관하게 바이오 인증 감시를 시작한다. */
    private fun requestNotificationPermissionThenWatch() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            PersonaWatchService.start(applicationContext)
            return
        }
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) PersonaWatchService.start(applicationContext)
        else notifPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private fun setupEdgeToEdge() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.parseColor("#070b09")
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS)
    }

    /**
     * setDecorFitsSystemWindows(false) 로 edge-to-edge 를 켰으면 **누군가는 인셋을 소비해야 한다**.
     * 아무도 안 하면 WebView 가 상태바 아래로 파고들어 웹 UI 상단이 시계·배터리와 겹친다.
     * 루트에 패딩으로 물려 WebView·프로그레스바·에러화면이 한꺼번에 시스템 바를 피하게 한다.
     * (노치 기기는 displayCutout 까지 함께 봐야 가로모드에서 안 잘린다.)
     */
    private fun applySystemBarInsets() {
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        ViewCompat.requestApplyInsets(binding.root)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        binding.webView.apply {
            setBackgroundColor(Color.parseColor("#070b09"))
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                useWideViewPort = true
                loadWithOverviewMode = true
                setSupportZoom(false)
                builtInZoomControls = false
                displayZoomControls = false
                cacheMode = WebSettings.LOAD_DEFAULT
                userAgentString = "$userAgentString GenomicWQBApp/1.0"
                mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            }
            CookieManager.getInstance().let {
                it.setAcceptCookie(true)
                it.setAcceptThirdPartyCookies(this, true)
            }
            webViewClient = AppWebViewClient()
            webChromeClient = AppChromeClient()
            isVerticalScrollBarEnabled = false
            isHorizontalScrollBarEnabled = false
            overScrollMode = View.OVER_SCROLL_NEVER
        }
    }

    private fun setupBackNavigation() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (binding.webView.canGoBack()) { binding.webView.goBack(); return }
                val now = System.currentTimeMillis()
                if (now - lastBackPressTime <= BACK_EXIT_WINDOW_MS) {
                    backPressToast?.cancel(); finish(); return
                }
                lastBackPressTime = now
                backPressToast?.cancel()
                backPressToast = Toast.makeText(
                    this@MainActivity, "한 번 더 누르면 종료됩니다", Toast.LENGTH_SHORT
                ).also { it.show() }
            }
        })
    }

    private fun loadUrl(url: String) {
        binding.errorContainer.visibility = View.GONE
        binding.webView.visibility = View.VISIBLE
        binding.webView.loadUrl(url)
    }

    private fun showErrorState() {
        binding.webView.visibility = View.GONE
        binding.errorContainer.visibility = View.VISIBLE
        binding.progressBar.visibility = View.GONE
    }

    private fun hideErrorState() {
        binding.errorContainer.visibility = View.GONE
        binding.webView.visibility = View.VISIBLE
    }

    private fun isNetworkAvailable(): Boolean {
        val cm = getSystemService(ConnectivityManager::class.java)
        val network = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(network) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(KEY_URL, binding.webView.url)
        binding.webView.saveState(outState)
    }

    override fun onRestoreInstanceState(savedInstanceState: Bundle) {
        super.onRestoreInstanceState(savedInstanceState)
        binding.webView.restoreState(savedInstanceState)
    }

    inner class AppWebViewClient : WebViewClient() {
        override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
            super.onPageStarted(view, url, favicon)
            binding.progressBar.visibility = View.VISIBLE
        }

        override fun onPageFinished(view: WebView?, url: String?) {
            super.onPageFinished(view, url)
            isPageLoaded = true
            binding.progressBar.visibility = View.GONE
            // 로그인 직후 쿠키를 디스크에 넘겨야 감시 서비스가 즉시 읽을 수 있다.
            CookieManager.getInstance().flush()
        }

        override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
            val url = request?.url?.toString() ?: return false
            if (url.contains(HOST)) return false
            // persona(withpersona.com) 인증은 카메라가 필요해 시스템 브라우저로 넘긴다.
            try {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            } catch (_: Exception) { /* 핸들러 없는 URL 무시 */ }
            return true
        }

        override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
            super.onReceivedError(view, request, error)
            if (request?.isForMainFrame == true) showErrorState()
        }
    }

    inner class AppChromeClient : WebChromeClient() {
        override fun onProgressChanged(view: WebView?, newProgress: Int) {
            super.onProgressChanged(view, newProgress)
            binding.progressBar.progress = newProgress
            if (newProgress >= 100) binding.progressBar.visibility = View.GONE
        }
    }
}
