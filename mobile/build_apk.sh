#!/usr/bin/env bash
# GenomicWQB Android APK 빌드 (sudo/docker 불필요)
#
# 방식: aarch64 호스트에서 JDK17·Gradle 은 네이티브로 돌리고, x86_64 전용인 aapt2 만
# qemu-user-static 으로 에뮬레이션한다(~/android-build/native/ 에 준비돼 있음).
# 자세한 배경은 ~/android-build/README.md 참고.
#
# 사용법:
#   ./build_apk.sh                        # debug APK 빌드
#   GENOMICWQB_DRIVE_UPLOAD=1 ./build_apk.sh   # 빌드 후 Google Drive 업로드
#
# 산출물: ./GenomicWQB.apk
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATIVE="${HOME}/android-build/native"
APK_OUT="${HERE}/GenomicWQB.apk"

log(){ printf '\033[1;36m[genomicwqb-apk]\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m[genomicwqb-apk] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -x "${NATIVE}/aapt2-wrap/aapt2" ] || die "네이티브 빌드 툴체인 없음: ${NATIVE} (jdk17/sdk/aapt2-wrap)"

export JAVA_HOME="${NATIVE}/jdk17"
export ANDROID_HOME="${NATIVE}/sdk"
cd "$HERE"
echo "sdk.dir=${ANDROID_HOME}" > local.properties

log "gradlew assembleDebug…"
# AGP 는 오버라이드 경로의 파일명이 정확히 'aapt2' 여야 받아들인다 (aapt2-wrap/aapt2)
./gradlew --no-daemon assembleDebug \
  -Pandroid.aapt2FromMavenOverride="${NATIVE}/aapt2-wrap/aapt2"

APK_SRC="app/build/outputs/apk/debug/app-debug.apk"
[ -f "$APK_SRC" ] || die "APK 미생성: $APK_SRC"
cp -f "$APK_SRC" "$APK_OUT"
log "완료: $(ls -lh "$APK_OUT" | awk '{print $9, "("$5")"}')"

if [ "${GENOMICWQB_DRIVE_UPLOAD:-0}" = "1" ]; then
  log "Google Drive 업로드 (apk/GenomicWQB.apk)…"
  rclone copyto "$APK_OUT" "gdrive:apk/GenomicWQB.apk" --progress \
    || log "⚠ Drive 업로드 실패 — 빌드 자체는 정상."
fi
