"""로그인 검증 — WQB 자격증명을 WQB REST API(POST /authentication)로 검증한다.

시뮬 백엔드는 REST API 단일이다 (2026-07-13 Playwright/브라우저 경로 제거).

각 단계마다 구체적 reason 코드를 반환:
  - wqb_credentials      : WQB API 401 — 이메일/비밀번호 거절
  - wqb_not_consultant   : WQB API 403 — 이 계정은 API 접근 불가
  - wqb_persona_required : biometric(Persona) 인증 필요 — 대시보드에서 1회 완료
  - wqb_rate_limited     : 429 — 인증 호출 분당 한도 초과
  - wqb_unreachable      : api.worldquantbrain.com 접속 실패 (서버/네트워크)
  - gemini_invalid/quota/network : (구 호출부 호환용) Gemini 키 검증 결과
  - ok                   : 통과
"""

from __future__ import annotations

import logging
from typing import Any

import requests as _requests
from requests.auth import HTTPBasicAuth as _HTTPBasicAuth

_WQB_API_BASE = 'https://api.worldquantbrain.com'

LOG = logging.getLogger('genomicwqb.auth')

# ─────────────────────────────────────────────────────────────────────────────
# Gemini 검증
# ─────────────────────────────────────────────────────────────────────────────

def validate_gemini_key(api_key: str) -> dict[str, Any]:
    """Gemini API 키를 가벼운 호출로 검증. 1회 generate_content 로 충분."""
    if not api_key or len(api_key) < 10:
        return {'ok': False, 'reason': 'gemini_invalid', 'detail': 'API 키가 비어있거나 너무 짧음'}
    try:
        # 로컬 LLM seam — Gemini 대신 로컬 Ollama 로 ping(외부 API 미사용, 2026-06-24).
        from . import local_llm as genai
        from .local_llm import types as genai_types
    except Exception as e:
        return {'ok': False, 'reason': 'gemini_network',
                'detail': f'local LLM seam 임포트 실패: {e}'}

    try:
        client = genai.Client(api_key=api_key)
        # 1 토큰만 생성 — 로컬 모델 가용성 확인용.
        resp = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents='ping',
            config=genai_types.GenerateContentConfig(max_output_tokens=4),
        )
        _ = (resp.text or '').strip()
        return {'ok': True, 'reason': 'ok'}
    except Exception as e:
        msg = str(e)
        ml = msg.lower()
        if any(s in ml for s in ('api key not valid', 'invalid api key', 'permission_denied',
                                  '401', '403', 'unauthenticated', 'unauthorized')):
            return {'ok': False, 'reason': 'gemini_invalid',
                    'detail': f'Gemini API 키가 유효하지 않습니다: {msg[:200]}'}
        if any(s in ml for s in ('quota', 'rate limit', 'resource_exhausted', '429')):
            return {'ok': False, 'reason': 'gemini_quota',
                    'detail': f'Gemini API 쿼터 초과: {msg[:200]}'}
        return {'ok': False, 'reason': 'gemini_network',
                'detail': f'Gemini 호출 실패: {msg[:300]}'}


# ─────────────────────────────────────────────────────────────────────────────
# WQB API 검증 (Research Consultant 계정 전용)
# ─────────────────────────────────────────────────────────────────────────────

def _api_post_auth(username: str, password: str):
    """POST /authentication 을 HTTPBasicAuth 로 호출. 테스트에서 monkeypatch 가능한 독립 함수."""
    sess = _requests.Session()
    sess.auth = _HTTPBasicAuth(username, password)
    return sess.post(_WQB_API_BASE + '/authentication', timeout=30)


# WQB 가 /authentication 200 바디에 실어 주는 권한 배열의 컨설턴트 표식.
# 실측(2026-07-27, RC 계정): permissions=[..., "CONSULTANT", "MULTI_SIMULATION", ...]
# /users/self 의 onboarding.status = "CONSULTANT_APPROVED" 도 같은 사실을 말한다.
CONSULTANT_PERMISSION = 'CONSULTANT'


def _permissions_of(r) -> list:
    """/authentication 응답의 permissions 배열. 없거나 못 읽으면 빈 리스트."""
    try:
        body = r.json()
    except Exception:
        return []
    perms = body.get('permissions') if isinstance(body, dict) else None
    return [str(p) for p in perms] if isinstance(perms, list) else []


def account_type_for(permissions) -> str:
    """WQB 권한 배열 → 우리 account_type.

    ⚠ 이 값은 **측정**이지 자기 신고가 아니다. 예전엔 가입 폼 라디오 버튼이 정했는데,
    일반 계정도 API Basic 인증이 통과하므로(auth 주석 참조) '로그인 되면 RC' 라는
    옛 승급 검사는 아무나 통과했다 — 즉 게이트 구실을 못 했다(2026-07-27).
    """
    return ('research_consultant'
            if CONSULTANT_PERMISSION in set(permissions or ()) else 'standard')


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
        return {'ok': True, 'reason': 'ok', 'detail': 'WQB API 인증 성공',
                'permissions': _permissions_of(r)}
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
# 통합 검증 — 두 단계 순차
# ─────────────────────────────────────────────────────────────────────────────

def probe_wqb_backend(username: str, password: str) -> dict[str, Any]:
    """POST /authentication 으로 이 계정의 **시뮬 백엔드 능력**을 측정한다.

    account_type(역할)과 분리된 개념이다. 과거 코드는 `403 → wqb_not_consultant` 로
    'API 는 RC 전용' 이라 **가정**했지만, WQB REST API 는 일반 계정도 이메일+비밀번호
    Basic 으로 붙는다(worldqt 레포·실측 확인). 그 가정을 측정으로 대체한다.

    반환:
      {'backend': 'api',          ...}  — 200/201 또는 401+persona (persona 는 계정 무관,
                                          일반 계정도 완료하면 API 를 쓴다)
      {'backend': 'api_forbidden', ...} — 403 (이 계정은 API 접근 불가; Playwright 경로가
                                          2026-07-13 제거돼 더는 브라우저로 폴백하지 않는다)
      {'backend': '',             ...}  — 자격증명 오류/429/도달불가 (persist 하지 말 것)
    각각 validate_wqb_api 의 reason·persona_url 을 그대로 싣는다.
    """
    v = validate_wqb_api(username, password)
    reason = v.get('reason')
    if v.get('ok') or reason == 'wqb_persona_required':
        # account_type 도 함께 싣는다 — 워커가 이 탐침 결과로 승급/강등을 동기화한다
        # (2026-07-27). 안 실으면 그 동기화가 조용히 아무 일도 안 한다.
        out = {'backend': 'api', **v}
        if v.get('ok'):
            out['account_type'] = account_type_for(v.get('permissions'))
        return out
    if reason == 'wqb_not_consultant':      # HTTP 403 — API 접근 불가(브라우저 폴백 없음)
        return {'backend': 'api_forbidden', **v}
    return {'backend': '', **v}             # 401/429/unreachable — 판정 보류


def validate_login(wqb_username: str, wqb_password: str, gemini_api_key: str = '',
                   account_type: str = 'standard') -> dict[str, Any]:
    """WQB 자격증명을 검증하고 **시뮬 백엔드 능력을 측정**해 함께 돌려준다.

    Gemini 검증은 2026-07-03 제거 — 알파 생성이 Genome GA 로 전환되어 LLM API 키가
    더 이상 필요 없다. gemini_api_key 는 구 호출부/테스트 호환용으로만 받고 무시한다.

    시뮬 백엔드는 이제 WQB REST API 단일이다(2026-07-13 Playwright 경로 제거). account_type
    은 등록 폼의 희망사항일 뿐, 실제 접속 가능 여부는 능력 탐침이 정한다:
      - API 로 붙으면(200/201/persona) → backend='api'
      - 403 → backend='api_forbidden' (이 계정은 WQB API 접근 불가 — 등록 거절)
    반환 dict 에 'backend' 키를 실어 호출부(app.register)가 users.backend 에 저장한다.
    """
    probe = probe_wqb_backend(wqb_username, wqb_password)
    backend = probe.get('backend')
    if backend == 'api':
        if not probe.get('ok'):
            return probe                      # persona_required 등 — 그대로 전달
        perms = probe.get('permissions') or []
        return {'ok': True, 'reason': 'ok', 'backend': 'api',
                'permissions': perms,
                'account_type': account_type_for(perms),
                'detail': 'WQB API 인증 통과'}
    if backend == 'api_forbidden':
        # 403 — 이 계정은 WQB REST API 를 못 쓴다. 브라우저 폴백은 제거됐으므로 거절.
        return {'ok': False, 'reason': 'wqb_not_consultant', 'backend': 'api_forbidden',
                'detail': 'WQB API 접근이 허용되지 않은 계정입니다(HTTP 403). '
                          'WorldQuant Brain 계정 권한을 확인해 주세요.'}
    # backend == '' (자격증명/429/도달불가) — 그대로 전달.
    return probe
