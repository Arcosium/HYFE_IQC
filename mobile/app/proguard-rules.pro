# Add project specific ProGuard rules here.
# Keep WebView JS interface
-keepclassmembers class uk.ai_ve.vcnews.WebAppInterface {
    @android.webkit.JavascriptInterface <methods>;
}

# Keep Kotlin metadata
-keepattributes *Annotation*
-keep class kotlin.Metadata { *; }
