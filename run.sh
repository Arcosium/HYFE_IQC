#!/usr/bin/env bash
# HYFE_IQC 서버 기동 스크립트.
#
# 사용법:
#   ./run.sh                  # 포그라운드 실행
#   ./run.sh background       # nohup 으로 백그라운드 실행 (logs/server.log)
#   HYFE_IQC_PORT=9000 ./run.sh   # 포트 변경
set -euo pipefail

cd "$(dirname "$0")"

# Flask + google-genai + cryptography 는 시스템 python3 (3.9) 사용.
PYTHON="${HYFE_PY:-/usr/bin/python3}"
# Playwright 서브프로세스는 python3.11.
export IQC_PY="${IQC_PY:-/usr/bin/python3.11}"

# ── 보안 기본값 ──────────────────────────────────────────────────────────
# 공개망 직접 노출 차단: Cloudflare 터널(iqc.ai-ve.uk → http://localhost:8088)
# 이 루프백으로 붙으므로 127.0.0.1 만 바인딩한다. 0.0.0.0 으로 띄우면 VM
# 공인 IP:8088 로 평문 직접 접근이 가능해 터널/TLS 가 우회된다.
# (LAN/공인 IP 직접 공개가 꼭 필요하면 호출 시 HYFE_IQC_HOST=0.0.0.0 override)
export HYFE_IQC_HOST="${HYFE_IQC_HOST:-127.0.0.1}"
# 터널이 HTTPS 종단이므로 세션 쿠키에 Secure 플래그 강제.
export HYFE_IQC_COOKIE_SECURE="${HYFE_IQC_COOKIE_SECURE:-1}"
# ─────────────────────────────────────────────────────────────────────────

# 의존성 자동 설치 (이미 깔려 있으면 빠르게 통과).
if ! "$PYTHON" -c "import flask, cryptography; from google import genai" 2>/dev/null; then
  echo "[run.sh] 시스템 python3 에 의존성 누락 — pip install --user 진행"
  "$PYTHON" -m pip install --user --upgrade flask google-genai cryptography python-dotenv
fi

# Playwright 인터프리터 검사 (없으면 경고만).
if ! "$IQC_PY" -c "import playwright" 2>/dev/null; then
  echo "[run.sh] ⚠ $IQC_PY 에 playwright 가 없습니다."
  echo "  sudo dnf install -y python3.11 python3.11-pip"
  echo "  $IQC_PY -m pip install --user playwright"
  echo "  $IQC_PY -m playwright install chromium"
fi

mode="${1:-foreground}"
case "$mode" in
  bg|background|nohup)
    echo "[run.sh] 백그라운드 실행 (logs/server.log)"
    mkdir -p logs
    nohup "$PYTHON" -m server.app > logs/server.log 2>&1 &
    echo "  pid=$!"
    ;;
  *)
    exec "$PYTHON" -m server.app
    ;;
esac
