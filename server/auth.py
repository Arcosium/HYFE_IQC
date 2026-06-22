"""로그인 검증 — WQB 자격증명만 검증.

각 단계마다 구체적 reason 코드를 반환:
  - wqb_credentials    : WQB 로그인 폼이 자격증명 거절 (이메일/비밀번호 오타)
  - wqb_unreachable    : platform.worldquantbrain.com 접속 자체 실패 (서버/네트워크)
  - wqb_captcha        : Cloudflare 등 봇 챌린지 발견
  - playwright_setup   : 브라우저 자체가 못 뜸
  - ok                 : 모두 통과
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import hashlib
import logging
from typing import Any

import requests as _requests
from requests.auth import HTTPBasicAuth as _HTTPBasicAuth

_WQB_API_BASE = 'https://api.worldquantbrain.com'

LOG = logging.getLogger('hyfe.auth')

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# IQC 와 동일한 정책: Playwright 는 python3.11 subprocess.
IQC_PYTHON = os.environ.get('IQC_PY') or '/usr/bin/python3.11'
if not os.path.exists(IQC_PYTHON):
    IQC_PYTHON = sys.executable or 'python3'

VALIDATE_TIMEOUT_SEC = 90  # WQB 로그인까지 90초.


def validate_gemini_key(_legacy_api_key: str = '') -> dict[str, Any]:
    """Deprecated compatibility shim. Local mode never validates or sends a key."""
    return {'ok': True, 'reason': 'ok'}


def _user_profile_dir(username: str) -> str:
    """user 별 격리된 chromium 프로필. 검증/실행 모두 같은 프로필을 재사용."""
    h = hashlib.sha1(username.encode('utf-8')).hexdigest()[:10]
    return os.path.expanduser(f'~/.hyfe_iqc_browser_{h}')


# ─────────────────────────────────────────────────────────────────────────────
# WQB API 검증 (Research Consultant 계정 전용)
# ─────────────────────────────────────────────────────────────────────────────

def _api_post_auth(username: str, password: str):
    """POST /authentication 을 HTTPBasicAuth 로 호출. 테스트에서 monkeypatch 가능한 독립 함수."""
    sess = _requests.Session()
    sess.auth = _HTTPBasicAuth(username, password)
    return sess.post(_WQB_API_BASE + '/authentication', timeout=30)


def _resolve_persona_url(api_url: str) -> str:
    """WQB '/authentication/persona?inquiry=...' 는 302 로 Persona 호스팅
    verify 페이지(worldquantbrain.withpersona.com)로 리다이렉트한다. 그 최종
    URL 을 따라가 반환한다. 네트워크 실패 등 어떤 문제든 입력 api_url 로 폴백.
    (단위테스트에서는 monkeypatch 로 대체해 네트워크를 타지 않게 한다.)"""
    try:
        rr = _requests.get(api_url, timeout=10, allow_redirects=False)
        loc = rr.headers.get('Location')
        if rr.status_code in (301, 302, 303, 307, 308) and loc and 'withpersona.com' in loc:
            return loc
    except Exception:
        pass
    return api_url


def validate_wqb_api(username: str, password: str) -> dict[str, Any]:
    """WQB API 직접 인증 (Research Consultant 전용).

    반환 reason 코드:
      - ok                  : 200/201
      - wqb_credentials     : 401 또는 빈 자격증명
      - wqb_not_consultant  : 403 — API 접근 권한 없음
      - wqb_unreachable     : 연결 실패 또는 기타 HTTP 오류
    """
    if not username or not password:
        return {'ok': False, 'reason': 'wqb_credentials', 'detail': '아이디/비밀번호 비어있음'}
    try:
        r = _api_post_auth(username, password)
    except Exception as e:
        return {'ok': False, 'reason': 'wqb_unreachable',
                'detail': f'API 인증 연결 실패: {type(e).__name__}: {e}'}
    if r.status_code in (200, 201):
        return {'ok': True, 'reason': 'ok', 'detail': 'WQB API 인증 성공'}
    body: dict = {}
    try:
        headers = getattr(r, 'headers', {}) or {}
        if (headers.get('Content-Type', '') or '').startswith('application/json'):
            body = r.json()
    except Exception:
        body = {}
    is_persona = (
        r.status_code == 401
        and (getattr(r, 'headers', {}) or {}).get('WWW-Authenticate', '').lower() == 'persona'
    ) or (isinstance(body, dict) and bool(body.get('inquiry')))
    if is_persona:
        inq = body.get('inquiry') if isinstance(body, dict) else None
        loc = (getattr(r, 'headers', {}) or {}).get('Location')
        if loc and loc.startswith('/'):
            # loc is root-relative and ALREADY includes '/authentication/...'
            # (e.g. '/authentication/persona?inquiry=...') — do NOT prepend
            # '/authentication' again (that double-path 404s).
            url = f'{_WQB_API_BASE}{loc}'
        elif inq:
            url = f'{_WQB_API_BASE}/authentication/persona?inquiry={inq}'
        else:
            url = f'{_WQB_API_BASE}/authentication'
        # Follow the WQB 302 to the Persona-hosted verify page (the real
        # biometric URL the user must open); fall back to the API url.
        url = _resolve_persona_url(url)
        return {'ok': False, 'reason': 'wqb_persona_required',
                'detail': 'WQB biometric(Persona) 인증 필요 — 대시보드에서 1회 완료하세요.',
                'persona_url': url, 'inquiry': inq}
    if r.status_code == 401:
        return {'ok': False, 'reason': 'wqb_credentials', 'detail': 'WQB API 401 — 자격증명 거절'}
    if r.status_code == 403:
        return {'ok': False, 'reason': 'wqb_not_consultant',
                'detail': 'WQB API 403 — Research Consultant 권한 없음(또는 API 미허용)'}
    if r.status_code == 429:
        return {'ok': False, 'reason': 'wqb_rate_limited',
                'detail': 'WQB API 인증 호출 한도(분당 5회) 초과 — 1분 후 다시 시도하세요.'}
    return {'ok': False, 'reason': 'wqb_unreachable', 'detail': f'WQB API 인증 HTTP {r.status_code}'}


# ─────────────────────────────────────────────────────────────────────────────
# WQB 로그인 검증 (Playwright headless)
# ─────────────────────────────────────────────────────────────────────────────

def _build_validate_script() -> str:
    """주어진 WQB 자격증명으로 platform.worldquantbrain.com 에 로그인 시도.

    출력: stdout 마지막 줄 `RESULT_JSON: {...}` 형식.
    """
    return r"""
import os, sys, json, traceback, time
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROFILE = os.environ['HYFE_PROFILE_DIR']
USERNAME = os.environ.get('WQB_USERNAME', '')
PASSWORD = os.environ.get('WQB_PASSWORD', '')

result = {'ok': False, 'reason': 'unknown', 'detail': '', 'final_url': ''}

def out():
    print('RESULT_JSON:', json.dumps(result, ensure_ascii=False), flush=True)

try:
    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE,
                headless=True,
                viewport={'width': 1280, 'height': 800},
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
        except Exception as e:
            result['reason'] = 'playwright_setup'
            result['detail'] = f'chromium launch 실패: {type(e).__name__}: {e}'
            out(); sys.exit(0)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(20000)

        try:
            page.goto('https://platform.worldquantbrain.com/', wait_until='domcontentloaded', timeout=30000)
        except PWTimeout:
            result['reason'] = 'wqb_unreachable'
            result['detail'] = 'platform.worldquantbrain.com 접속 timeout (30s)'
            try: ctx.close()
            except Exception: pass
            out(); sys.exit(0)
        except Exception as e:
            result['reason'] = 'wqb_unreachable'
            result['detail'] = f'navigate 실패: {type(e).__name__}: {e}'
            try: ctx.close()
            except Exception: pass
            out(); sys.exit(0)

        page.wait_for_timeout(2500)

        # cookie consent / 광고 banner 등을 한 번 dismiss (로그인 폼이 가려질 수 있음).
        try:
            page.evaluate(r'''() => {
                const RX = /\b(Skip|Got it|Exit|Continue|Close|Dismiss|OK|Okay|Accept|Agree|Allow|Done|Next|Later|Confirm|확인|동의|허용|닫기|건너뛰기|나중에)\b/i;
                [...document.querySelectorAll('button, a[role="button"], [role="button"]')].forEach(b => {
                    try {
                        if (b.offsetParent === null) return;
                        const t = ((b.innerText || b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
                        if (t && RX.test(t)) b.click();
                    } catch(e) {}
                });
            }''')
            page.wait_for_timeout(600)
        except Exception:
            pass

        body = (page.locator('body').inner_text(timeout=5000) or '').lower()
        if any(s in body for s in ('access denied', 'cloudflare', 'attention required', 'unusual traffic')):
            result['reason'] = 'wqb_captcha'
            result['detail'] = 'WQB 접속 시 봇 챌린지/Cloudflare 페이지 감지'
            try: ctx.close()
            except Exception: pass
            out(); sys.exit(0)

        # 이미 로그인 세션이 살아 있다면 (이전 검증 후 프로필 재사용) 로그인 폼이 안 뜬다.
        has_login_form = page.locator('input[type="password"]').count() > 0

        if has_login_form:
            try:
                page.locator('input[type="email"], input[name="email"], input[type="text"]').first.fill(USERNAME, timeout=8000)
                page.locator('input[type="password"]').first.fill(PASSWORD, timeout=5000)
                # submit 버튼 (text "Sign in" / "Log in" / type=submit)
                clicked = False
                for sel in ['button[type="submit"]', 'button:has-text("Sign in")',
                            'button:has-text("Log in")', 'button:has-text("Login")']:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            loc.click(timeout=4000)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    page.keyboard.press('Enter')
            except Exception as e:
                result['reason'] = 'wqb_unreachable'
                result['detail'] = f'로그인 폼 필드 fill 실패: {type(e).__name__}: {e}'
                try: ctx.close()
                except Exception: pass
                out(); sys.exit(0)

            # 로그인 처리 결과 대기 — 성공 시 URL 변경 또는 dashboard, 실패 시 에러 메시지.
            deadline = time.time() + 25
            success = False
            error_msg = ''
            while time.time() < deadline:
                page.wait_for_timeout(1200)
                cur_url = page.url
                # 성공 신호: URL 이 '/login' 에서 벗어남 OR 로그인 폼이 사라짐 OR 'Simulate' 메뉴 보임
                if 'login' not in cur_url.lower() and page.locator('input[type="password"]').count() == 0:
                    success = True
                    break
                # 실패 메시지 검색
                try:
                    body_text = page.locator('body').inner_text(timeout=2000) or ''
                    bl = body_text.lower()
                    for kw in ('invalid', 'incorrect', 'wrong password', 'failed to', 'not match',
                               'unauthorized', 'authentication failed', 'no account'):
                        if kw in bl:
                            # 메시지 한 줄 추출
                            for line in body_text.splitlines():
                                if kw in line.lower():
                                    error_msg = line.strip()[:200]
                                    break
                            break
                    if error_msg:
                        break
                except Exception:
                    pass
            if not success and error_msg:
                result['reason'] = 'wqb_credentials'
                result['detail'] = f'WQB 로그인 거절: {error_msg}'
                try: ctx.close()
                except Exception: pass
                out(); sys.exit(0)
            if not success:
                # 폼은 사라졌는데 URL 이 바뀌지 않은 경우 등 → URL 기반 재판정
                cur_url = page.url
                if page.locator('input[type="password"]').count() > 0:
                    result['reason'] = 'wqb_credentials'
                    result['detail'] = '로그인 시도 후에도 비밀번호 폼이 그대로 남아 있음 — 자격증명 거절로 판정'
                    try: ctx.close()
                    except Exception: pass
                    out(); sys.exit(0)
                # 그 외에는 성공으로 간주 (페이지가 다른 상태로 갔으면 OK).

        # /simulate 페이지로 이동 가능한지 추가 확인 — 정상 로그인이면 simulate 가 보여야 함.
        try:
            page.goto('https://platform.worldquantbrain.com/simulate', wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(3500)
            if page.locator('input[type="password"]').count() > 0:
                result['reason'] = 'wqb_credentials'
                result['detail'] = '/simulate 진입 시 다시 로그인 폼 — 세션이 유지되지 않음 (자격증명 의심)'
                try: ctx.close()
                except Exception: pass
                out(); sys.exit(0)
            # 신규 디바이스 인증 페이지 감지 — 자동화 불가능, 사용자 안내.
            try:
                txt = (page.locator('body').inner_text(timeout=3000) or '').lower()
                url = (page.url or '').lower()
                auth_hits = [
                    p for p in ('verification code', 'verify your identity', 'two-factor',
                                'two factor', 'mfa code', 'authenticator', 'security code',
                                'new device', 'unrecognized device',
                                '2단계', '인증 코드', '본인 확인', '디바이스')
                    if p in txt
                ]
                if auth_hits or any(s in url for s in ('/verify', '/2fa', '/mfa', '/otp', '/challenge')):
                    result['reason'] = 'wqb_auth_required'
                    result['detail'] = (
                        f'WQB 가 새 디바이스 인증을 요구함 (감지: {auth_hits or "URL"}). '
                        '사용자가 한 번 수동으로 platform.worldquantbrain.com 에 로그인 후 인증 코드를 입력해 주세요. '
                        '인증을 마친 뒤 다시 시도하면 자동 로그인 가능합니다.'
                    )
                    try: ctx.close()
                    except Exception: pass
                    out(); sys.exit(0)
            except Exception:
                pass
            result['final_url'] = page.url
        except Exception as e:
            result['reason'] = 'wqb_unreachable'
            result['detail'] = f'/simulate 로드 실패: {type(e).__name__}: {e}'
            try: ctx.close()
            except Exception: pass
            out(); sys.exit(0)

        result['ok'] = True
        result['reason'] = 'ok'
        result['detail'] = '로그인 + /simulate 진입 성공'
        try: ctx.close()
        except Exception: pass

except Exception as e:
    traceback.print_exc()
    result['reason'] = 'playwright_setup'
    result['detail'] = f'{type(e).__name__}: {e}'

out()
"""


def validate_wqb_login(username: str, password: str) -> dict[str, Any]:
    """WQB 로그인 검증."""
    if not username or not password:
        return {'ok': False, 'reason': 'wqb_credentials',
                'detail': '아이디 또는 비밀번호가 비어있음'}

    if not os.path.exists(IQC_PYTHON):
        return {'ok': False, 'reason': 'playwright_setup',
                'detail': f'IQC Python 인터프리터 없음: {IQC_PYTHON}'}

    profile_dir = _user_profile_dir(username)
    os.makedirs(profile_dir, exist_ok=True)

    env = os.environ.copy()
    env['HYFE_PROFILE_DIR'] = profile_dir
    env['WQB_USERNAME'] = username
    env['WQB_PASSWORD'] = password
    tmp = os.path.expanduser('~/.hyfe_iqc_tmp')
    os.makedirs(tmp, exist_ok=True)
    env['TMPDIR'] = tmp
    env['XDG_RUNTIME_DIR'] = env.get('XDG_RUNTIME_DIR') or tmp

    try:
        proc = subprocess.run(
            [IQC_PYTHON, '-c', _build_validate_script()],
            capture_output=True, text=True,
            timeout=VALIDATE_TIMEOUT_SEC,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'reason': 'wqb_unreachable',
                'detail': f'WQB 검증 timeout ({VALIDATE_TIMEOUT_SEC}s)'}
    except Exception as e:
        return {'ok': False, 'reason': 'playwright_setup',
                'detail': f'서브프로세스 실행 실패: {type(e).__name__}: {e}'}

    out = proc.stdout or ''
    err = proc.stderr or ''
    # 마지막 RESULT_JSON 줄 파싱.
    parsed = None
    for line in reversed(out.splitlines()):
        s = line.strip()
        i = s.find('RESULT_JSON:')
        if i < 0:
            continue
        try:
            parsed = json.loads(s[i + len('RESULT_JSON:'):].strip())
            break
        except Exception:
            continue
    if parsed is None:
        tail = (err or out)[-1500:]
        return {'ok': False, 'reason': 'playwright_setup',
                'detail': f'RESULT_JSON 파싱 실패. 출력 tail: {tail}'}
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# 통합 검증 — 두 단계 순차
# ─────────────────────────────────────────────────────────────────────────────

def validate_login(wqb_username: str, wqb_password: str, _legacy_api_key: str | None = None,
                   account_type: str = 'standard') -> dict[str, Any]:
    """WQB 인증이 통과하면 ok를 반환한다.

    account_type='research_consultant' 이면 WQB API 직접 인증,
    그 외(기본값 'standard')는 Playwright 브라우저 인증.
    """
    # WQB — account_type 에 따라 분기.
    if account_type == 'research_consultant':
        w = validate_wqb_api(wqb_username, wqb_password)
    else:
        w = validate_wqb_login(wqb_username, wqb_password)
    if not w.get('ok'):
        return w
    return {'ok': True, 'reason': 'ok',
            'detail': f'WQB({account_type}) 인증 통과. WQB final_url={w.get("final_url","")}'}
