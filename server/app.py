"""HYFE_IQC Flask 앱 — 멀티유저 WQB 알파 백테스팅.

라우트:
  POST /api/login        — 자격증명 검증, 세션 쿠키 발급
  POST /api/logout       — 세션 폐기
  GET  /api/me           — 현재 세션 정보
  POST /api/start        — 워커 시작 (이미 떠있으면 noop)
  POST /api/pause        — 워커 일시정지 (즉시 종료)
  GET  /api/status       — 진행 상태 폴링
  GET  /api/logs         — since_id 이후 로그 (REST)
  GET  /api/logs/stream  — Server-Sent Events 로 실시간 로그
  GET  /api/rounds       — 라운드 히스토리
  GET  /api/best         — Submitted 알파 목록 (Submit 성공만)
  GET  /                 — 정적 index.html
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import logging
from typing import Any

import threading

from flask import Flask, jsonify, make_response, request, Response, send_from_directory

from . import auth as _auth
from . import db as _db
from . import worker as _worker
from . import run_config as _run_config
from . import wqb_data_service as _wqb_data_service

LOG = logging.getLogger('hyfe.app')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.abspath(os.path.join(_THIS_DIR, '..', 'static'))

app = Flask(__name__, static_folder=None)
app.config['JSON_AS_ASCII'] = False
SESSION_COOKIE = 'hyfe_session'

# Cloudflare HTTPS 등 신뢰 가능한 reverse proxy 통과 시 secure cookie + SameSite 설정.
# 운영 환경 (https://iqc.ai-ve.uk 같은) 에선 HYFE_IQC_COOKIE_SECURE=1 로 띄울 것.
COOKIE_SECURE = os.environ.get('HYFE_IQC_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes')
COOKIE_SAMESITE = os.environ.get('HYFE_IQC_COOKIE_SAMESITE') or ('None' if COOKIE_SECURE else 'Lax')

# /api/login 동시 호출 보호 — 같은 wqb_username 으로 중복 검증 시작 방지 (chromium subprocess 자원 보호).
_LOGIN_LOCK_LOCK = threading.Lock()
_LOGIN_IN_FLIGHT: set[str] = set()

# 주의: _db.init() / _auto_resume_workers() 는 import 부작용으로 두지 않는다.
# (모듈 import 만으로 DB 생성·워커 기동 → 테스트 불가, WSGI 다중워커서 중복
#  resume). 둘 다 main() 에서 1회 수행. _db.* 호출은 내부적으로 init() 을
#  lazy 하게 부르므로 라우트 동작에는 영향 없음.


def _ensure_house_rc_account() -> None:
    """하우스 RC 계정이 DB에 있으면 research_consultant로 보정 (데이터 서비스가
    그 계정의 API 세션으로 라이브 data-fields를 받음). 어떤 실패도 부팅을 막지 않는다."""
    try:
        hid = _db.get_user_id_by_username(_wqb_data_service.HOUSE_RC_USERNAME)
        if hid and _db.get_account_type(hid) != 'research_consultant':
            _db.set_account_type(hid, 'research_consultant')
            LOG.info('house RC 계정 보정: %s', _wqb_data_service.HOUSE_RC_USERNAME)
    except Exception as e:
        LOG.warning('house RC 보정 skip: %s', e)


def _auto_resume_workers() -> None:
    """서버 boot 시 running=1 인 사용자 워커 자동 시작 — 의도치 않은 재시작/배포 후
    사용자가 다시 '시작' 누르지 않아도 작업 이어가도록.
    paused=1 인 경우는 그대로 둔다 (사용자 의지로 멈췄으므로)."""
    try:
        uids = _db.list_running_user_ids()
        for uid in uids:
            try:
                w = _worker.get_or_create(uid)
                if not w.is_alive():
                    w.start()
                    LOG.info('auto-resume worker for user_id=%d', uid)
            except Exception as e:
                LOG.warning('auto-resume failed for user_id=%d: %s', uid, e)
    except Exception as e:
        LOG.warning('auto-resume sweep failed: %s', e)


# ─────────────────────────────────────────────────────────────────────────────
# 공용 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _int_arg(name: str, default: int) -> int:
    """request.args 의 정수 쿼리 파라미터 안전 파싱 (비정상 입력 → default).

    기존 `int(request.args.get('x','N') or N)` 패턴은 `?x=abc` 에서
    ValueError → 처리 안 된 500. 한 곳으로 모아 견고하게.
    """
    try:
        v = request.args.get(name)
        return int(v) if v not in (None, '') else int(default)
    except (TypeError, ValueError):
        return int(default)


def _status_payload(uid: int) -> dict[str, Any]:
    """SSE/REST 공용 상태 dict — DB 상태 + in-memory 워커 살아있음 + 카운트.
    thread_alive 가 두 경로에서 동일해야 클라이언트 버튼 상태가 깜빡이지 않음."""
    s = _db.get_user_status(uid)
    w = _worker.get(uid)
    s['thread_alive'] = bool(w and w.is_alive())
    s['paused_in_memory'] = bool(w and w.is_paused())
    s['errors_count'] = _db.total_errors_count(uid)
    s['latest_log_id'] = _db.latest_log_id(uid)
    s['last_cleared_log_id'] = _db.get_last_cleared_log_id(uid)
    account_type = _db.get_account_type(uid)
    is_rc = account_type == 'research_consultant'
    s['account_type'] = account_type
    s['genome_model'] = 'rc-api-genome' if is_rc else 'standard-playwright-genome'
    s['backtester_mode'] = 'WQB API concurrent' if is_rc else 'Playwright browser'
    s['save_policy'] = (
        'RC: completed alpha마다 API 제출 시도 (직렬화 + 429 재시도)'
        if is_rc else
        'Non-RC: 7 basic PASS + self-correlation ≤ 0.7 알파만 저장'
    )
    # GA(유전 알고리즘) 상태 — UI Evolution 패널이 소비.
    try:
        seed_pool = len(_db.best_alphas_for_seeding(uid, top_n=5, min_pass_count=5))
    except Exception:
        seed_pool = 0
    try:
        focus_len = len(_db.get_focus_queue(uid))
    except Exception:
        focus_len = 0
    bandit_on = _run_config.is_bandit_enabled()
    s['ga'] = {'seed_pool': seed_pool, 'focus_queue': focus_len, 'bandit': bandit_on}
    s['ga_policy'] = (
        f'엘리트 seed {seed_pool}개 교차/변이 + 탐색'
        if seed_pool else '무작위 탐색 (엘리트 seed 수집 전)'
    )
    return s


# ─────────────────────────────────────────────────────────────────────────────
# 세션 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _current_user_id() -> int | None:
    token = request.cookies.get(SESSION_COOKIE) or ''
    if not token:
        token = (request.headers.get('X-Session') or '').strip()
    return _db.lookup_session(token) if token else None


def _require_user() -> tuple[int, None] | tuple[None, Response]:
    uid = _current_user_id()
    if uid is None:
        return None, _err('unauthorized', '로그인이 필요합니다', 401)
    return uid, None


def _err(reason: str, detail: str = '', status: int = 400) -> Response:
    resp = jsonify({'ok': False, 'reason': reason, 'detail': detail})
    resp.status_code = status
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# 정적 파일
# ─────────────────────────────────────────────────────────────────────────────

_MOBILE_UA_RX = re.compile(
    r'(iPhone|iPod|Android.*Mobile|webOS|BlackBerry|IEMobile|Opera Mini)',
    re.IGNORECASE,
)


def _is_mobile_ua(ua: str) -> bool:
    return bool(ua) and bool(_MOBILE_UA_RX.search(ua))


def _no_cache_html(name: str) -> Response:
    """HTML 자체는 캐시 금지 — 그 안에서 참조하는 app.js?v=N 이 최신이어야 캐시-무효화가
    실제로 작동하기 때문. (HTML 이 stale 이면 v= 번호가 안 올라감.)"""
    resp = make_response(send_from_directory(STATIC_DIR, name))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/', methods=['GET'])
def index():
    # ?desktop=1 강제 desktop. 모바일 UA 면 mobile.html 자동 서빙.
    if request.args.get('desktop'):
        return _no_cache_html('index.html')
    if _is_mobile_ua(request.headers.get('User-Agent', '')):
        return _no_cache_html('mobile.html')
    return _no_cache_html('index.html')


@app.route('/m', methods=['GET'])
@app.route('/mobile', methods=['GET'])
def mobile_index():
    return _no_cache_html('mobile.html')


@app.route('/<path:path>', methods=['GET'])
def static_files(path: str):
    if path.startswith('api/'):
        # /api/* 는 위 라우트가 핸들; 여기 들어오면 미정의 path → 404
        return _err('not_found', f'unknown api path: {path}', 404)
    full = os.path.join(STATIC_DIR, path)
    if not os.path.isfile(full):
        # SPA-style 라우팅 — 정적 파일 아니면 index 로 폴백 (해시 라우팅이라 사실 사용 안 함).
        return _no_cache_html('index.html')
    if path.endswith('.html'):
        return _no_cache_html(path)
    # JS/CSS 등 정적 자산: ETag/Last-Modified 기반 *재검증 강제*.
    # 'no-cache' = 매 요청 조건부 검증(미변경이면 304, 변경 즉시 반영) →
    # 수동 ?v= 캐시버스터가 필요 없어진다. send_from_directory 가 ETag/
    # Last-Modified + 조건부 304 를 이미 처리하므로 헤더만 보강.
    resp = make_response(send_from_directory(STATIC_DIR, path))
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# 인증
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def api_login():
    """기존 사용자 전용 로그인 — 아이디/비밀번호만 확인한다.

    Gemini 키 검증은 2026-07-03 제거 (알파 생성이 Genome GA 로 전환되어 LLM 키 불필요).
    body 에 gemini_api_key 가 와도 무시한다 (구 클라이언트 호환).
    신규 가입은 /api/register 를 사용할 것.
    """
    body = request.get_json(silent=True) or {}
    wqb_username = (body.get('wqb_username') or '').strip()
    wqb_password = (body.get('wqb_password') or '').strip()
    remember = bool(body.get('remember', True))

    if not (wqb_username and wqb_password):
        return _err('missing_fields', '아이디 / 비밀번호가 필요합니다', 400)

    existing = _db.find_user_by_username(wqb_username)

    if not existing:
        return _err('not_registered',
                    '가입되지 않은 계정입니다. 회원가입을 먼저 해주세요.', 404)

    if existing['wqb_password'] != wqb_password:
        LOG.info('login fail (password mismatch for existing user): %s', wqb_username)
        return _err('wqb_credentials',
                    '저장된 자격증명과 일치하지 않습니다. WQB 비밀번호를 변경했다면 '
                    '관리자에게 record 초기화를 요청하세요.', 401)

    uid = int(existing['id'])
    _db.update_user_secrets(uid)  # last_login_at touch
    return _issue_session(uid, wqb_username, remember)


@app.route('/api/register', methods=['POST'])
def api_register():
    """신규 사용자 전용 가입 — WQB 자격증명만 검증한다.

    이미 등록된 username 이면 409. account_type 화이트리스트 적용
    (standard/research_consultant, 그 외는 standard 로 강등).
    Gemini API 키는 2026-07-03부터 받지 않는다 (Genome GA 전환으로 불필요).
    기존 사용자 로그인은 /api/login 을 사용할 것.
    """
    body = request.get_json(silent=True) or {}
    wqb_username = (body.get('wqb_username') or '').strip()
    wqb_password = (body.get('wqb_password') or '').strip()
    remember = bool(body.get('remember', True))
    account_type = (body.get('account_type') or 'standard').strip()
    if account_type not in ('standard', 'research_consultant'):
        account_type = 'standard'

    if not (wqb_username and wqb_password):
        return _err('missing_fields', '아이디 / 비밀번호가 필요합니다', 400)

    if _db.find_user_by_username(wqb_username):
        return _err('already_registered',
                    '이미 가입된 계정입니다. 로그인해 주세요.', 409)

    # chromium subprocess 보호용 in-flight 락 (같은 username 이 동시에
    # 두 번 풀 검증을 트리거하지 않도록).
    with _LOGIN_LOCK_LOCK:
        if wqb_username in _LOGIN_IN_FLIGHT:
            return _err('login_in_progress',
                        '이미 같은 WQB 아이디로 검증이 진행 중입니다. 잠시 후 다시 시도해 주세요.',
                        429)
        _LOGIN_IN_FLIGHT.add(wqb_username)

    try:
        LOG.info('register attempt (WQB validation): %s', wqb_username)
        result = _auth.validate_login(wqb_username, wqb_password, account_type=account_type)
    finally:
        with _LOGIN_LOCK_LOCK:
            _LOGIN_IN_FLIGHT.discard(wqb_username)

    if not result.get('ok'):
        LOG.info('register fail %s: %s — %s',
                 wqb_username, result.get('reason'), result.get('detail'))
        return jsonify(result), 401

    uid = _db.upsert_user(wqb_username, wqb_password, '', account_type=account_type)
    return _issue_session(uid, wqb_username, remember)


@app.route('/api/m_login', methods=['POST'])
def api_m_login():
    """모바일 light 로그인 — 아이디 / 비밀번호만으로 세션 발급.

    이미 등록된 사용자만 통과 (Gemini 키는 신규 가입 / 갱신 시점에만 필요).
    워커는 desktop 에서 시작/제어하고, 모바일은 진행 상황만 본다.
    """
    body = request.get_json(silent=True) or {}
    wqb_username = (body.get('wqb_username') or '').strip()
    wqb_password = (body.get('wqb_password') or '').strip()
    if not (wqb_username and wqb_password):
        return _err('missing_fields', '아이디 / 비밀번호가 필요합니다.', 400)
    existing = _db.find_user_by_username(wqb_username)
    if not existing:
        return _err('not_registered',
                    '등록되지 않은 사용자입니다. 데스크톱에서 Gemini 키를 포함해 한 번 가입하세요.',
                    401)
    if existing.get('wqb_password') != wqb_password:
        return _err('wqb_credentials', '비밀번호가 일치하지 않습니다.', 401)
    uid = int(existing['id'])
    _db.update_user_secrets(uid)  # last_login_at touch
    LOG.info('m_login ok: user_id=%d (%s)', uid, wqb_username)
    return _issue_session(uid, wqb_username, remember=True)


def _issue_session(uid: int, wqb_username: str, remember: bool) -> Response:
    """세션 발급 + 쿠키 세팅 + 응답."""
    token = «REDACTED»
    resp = jsonify({'ok': True, 'reason': 'ok', 'user_id': uid,
                    'wqb_username': wqb_username})
    cookie_kwargs = dict(
        httponly=True, samesite=COOKIE_SAMESITE, secure=COOKIE_SECURE, path='/',
    )
    if remember:
        cookie_kwargs['max_age'] = _db.SESSION_TTL_SEC  # 7일 영속 쿠키.
    # remember=False 면 max_age 생략 → 브라우저 세션 쿠키 (탭 닫으면 사라짐).
    resp.set_cookie(SESSION_COOKIE, token, **cookie_kwargs)
    LOG.info('login ok: user_id=%d (remember=%s, cookie secure=%s samesite=%s)',
             uid, remember, COOKIE_SECURE, COOKIE_SAMESITE)
    return resp


@app.route('/api/account/upgrade-to-rc', methods=['POST'])
def api_upgrade_to_rc():
    """Research Consultant 계정으로 전환 — WQB API 로 RC 자격 확인 후 account_type 갱신."""
    uid = _current_user_id()
    if not uid:
        return _err('not_logged_in', '로그인이 필요합니다', 401)
    creds = _db.get_user_credentials(uid)
    if not creds:
        return _err('no_credentials', '자격증명을 찾을 수 없습니다', 400)
    username, password, _ = creds
    v = _auth.validate_wqb_api(username, password)
    if not v.get('ok'):
        return jsonify(v), 400
    _db.set_account_type(uid, 'research_consultant')
    return jsonify({'ok': True, 'account_type': 'research_consultant'})


@app.route('/api/account/wqb-persona-status', methods=['GET'])
def api_wqb_persona_status():
    """현재 사용자의 WQB 페르소나 완료 여부 확인.

    이 엔드포인트는 **읽기 전용**이다 — 저장된 세션과 .pending 파일만 본다.
    절대 passive 하게 POST /authentication 을 하지 않는다: 매 조회마다 POST 하면
    미완료 persona 상태에서 WQB 가 BIOMETRICS_THROTTLED(429)를 매번 재무장시켜
    throttle 가 영원히 안 풀린다(사장님 "버튼 안 눌림" 버그의 근본 원인).
    세션도 pending challenge 도 없을 때만 인증 1회로 신규 challenge 를 발급한다.
    """
    uid = _current_user_id()
    if not uid:
        return _err('not_logged_in', '로그인이 필요합니다', 401)
    creds = _db.get_user_credentials(uid)
    if not creds:
        return _err('no_credentials', '자격증명을 찾을 수 없습니다', 400)
    u, p, _ = creds
    account_type = _db.get_account_type(uid)
    # 1) 저장된 세션이 살아있으면 biometric 불필요 — POST /authentication 호출 없이 반환.
    try:
        from .wqb_api import WqbApiClient, _is_public_persona_url
        c = WqbApiClient(u, p)
        if c._load_session() and c._session_valid():
            return jsonify({'persona_required': False, 'authenticated': True,
                            'account_type': account_type})
        # 1.5) 미완료 persona challenge(.pending)가 있으면 그 URL 을 POST 없이 반환.
        #      사용자가 브라우저에서 완료 후 '완료' 버튼을 누르면 그때 complete_persona()
        #      가 단 한 번 POST 한다. 여기서 POST 하면 throttle 재무장 루프가 된다.
        #      persona_url 이 빈 값(일시 해석 실패)이어도 pending 이 존재하는 한 새
        #      challenge 를 발급하지 않는다 — UI 가 "링크 준비 중" 을 띄우고 재시도.
        pend = c.pending_persona()
        if pend is not None:
            pu = pend.get('persona_url', '')
            if not _is_public_persona_url(pu):
                pu = ''
            return jsonify({'persona_required': True,
                            'persona_url': pu,
                            'inquiry': pend.get('inquiry', ''),
                            'account_type': account_type})
    except Exception:
        pass
    # 2) 세션도 pending 도 없음 → WqbApiClient 로 인증 1회 호출해 신규
    # challenge 를 발급한다. 이 경로는 challenge 쿠키를 .pending 에 저장해야
    # 완료 버튼에서 같은 WQB 세션으로 finalize 할 수 있다.
    try:
        c = WqbApiClient(u, p)
        if c.authenticate():
            return jsonify({'persona_required': False, 'authenticated': True,
                            'account_type': account_type})
        if c.persona_required:
            pend = c.pending_persona() or {}
            persona_url = pend.get('persona_url') or ''
            if not _is_public_persona_url(persona_url):
                persona_url = ''
            inquiry = pend.get('inquiry', '')
            if not inquiry and persona_url:
                try:
                    from urllib.parse import urlparse, parse_qs
                    q = parse_qs(urlparse(persona_url).query)
                    inquiry = (q.get('inquiry') or q.get('inquiry-id') or [''])[0]
                except Exception:
                    inquiry = ''
            return jsonify({'persona_required': True,
                            'persona_url': persona_url,
                            'inquiry': inquiry,
                            'account_type': account_type})
        if getattr(c, 'last_auth_status_code', None) == 429:
            return jsonify({'persona_required': False, 'rate_limited': True,
                            'detail': 'WQB API 인증 호출 한도(분당 5회) 초과 — 1분 후 다시 시도하세요.',
                            'account_type': account_type})
    except Exception:
        pass
    return jsonify({'persona_required': False, 'ok': False,
                    'account_type': account_type})


@app.route('/api/account/wqb-persona-complete', methods=['POST'])
def api_wqb_persona_complete():
    """사용자가 브라우저에서 페르소나 완료 후 세션을 저장한다."""
    uid = _current_user_id()
    if not uid:
        return _err('not_logged_in', '로그인이 필요합니다', 401)
    creds = _db.get_user_credentials(uid)
    if not creds:
        return _err('no_credentials', '자격증명을 찾을 수 없습니다', 400)
    u, p, _ = creds
    inquiry = ((request.get_json(silent=True) or {}).get('inquiry') or '').strip() or None
    from .wqb_api import WqbApiClient
    try:
        ok = WqbApiClient(u, p).complete_persona(inquiry=inquiry)
    except Exception as e:
        return _err('persona_failed', f'완료 처리 실패: {e}', 400)
    return jsonify({'ok': bool(ok)})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE) or ''
    if token:
        «REDACTED»
    resp = jsonify({'ok': True})
    resp.delete_cookie(SESSION_COOKIE, path='/')
    return resp


@app.route('/api/me', methods=['GET'])
def api_me():
    uid = _current_user_id()
    if uid is None:
        return _err('unauthorized', '', 401)
    u = _db.get_user(uid)
    if not u:
        return _err('unauthorized', 'user not found', 401)
    s = _db.get_user_status(uid)
    account_type = _db.get_account_type(uid)
    return jsonify({
        'ok': True,
        'user_id': uid,
        'wqb_username': u['wqb_username'],
        'account_type': account_type,
        **s,
    })


# ─────────────────────────────────────────────────────────────────────────────
# 워커 제어
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/start', methods=['POST'])
def api_start():
    uid, err = _require_user()
    if err:
        return err
    w = _worker.get(uid)
    if w and w.is_alive():
        # 이미 진행 중. paused 상태였다면 resume 으로 처리 — 새 thread 시작 필요.
        if w.is_paused():
            w.request_resume()
            # 죽은 thread 면 새로 만들어 시작.
            if not w.is_alive():
                new_w = _worker.get_or_create(uid)
                if not new_w.is_alive():
                    new_w.start()
            return jsonify({'ok': True, 'state': 'resumed'})
        return jsonify({'ok': True, 'state': 'already_running'})
    new_w = _worker.get_or_create(uid)
    if not new_w.is_alive():
        new_w.start()
    return jsonify({'ok': True, 'state': 'started'})


@app.route('/api/pause', methods=['POST'])
def api_pause():
    uid, err = _require_user()
    if err:
        return err
    w = _worker.get(uid)
    if not w or not w.is_alive():
        _db.set_user_running(uid, running=False, paused=False)
        return jsonify({'ok': True, 'state': 'not_running'})
    w.request_pause()
    return jsonify({'ok': True, 'state': 'pausing'})


@app.route('/api/status', methods=['GET'])
def api_status():
    uid, err = _require_user()
    if err:
        return err
    return jsonify({'ok': True, **_status_payload(uid)})


@app.route('/api/delay_mode', methods=['GET', 'POST'])
def api_delay_mode():
    """delay 테스트 모드 조회/변경. '0' | '1' | 'mix'. 워커가 매 라운드 새로 읽어
    재시작 없이 다음 라운드부터 반영된다."""
    uid, err = _require_user()
    if err:
        return err
    if request.method == 'GET':
        return jsonify({'ok': True, 'mode': _run_config.get_delay_mode()})
    mode = str((request.get_json(silent=True) or {}).get('mode', '')).strip().lower()
    try:
        saved = _run_config.set_delay_mode(mode)
    except ValueError:
        return _err('bad_mode', "mode 는 '0' | '1' | 'mix' 중 하나여야 합니다.", 400)
    return jsonify({'ok': True, 'mode': saved})


@app.route('/api/m_recent', methods=['GET'])
def api_m_recent():
    """모바일용 — 최근 N개 알파의 카운트 요약."""
    uid, err = _require_user()
    if err:
        return err
    limit = _int_arg('limit', 30)
    return jsonify({'ok': True, 'alphas': _db.list_recent_alpha_summaries(uid, limit=limit)})


@app.route('/api/m_submits', methods=['GET'])
def api_m_submits():
    """모바일용 — 최근 '제출 시도' 알파 (라운드 종료 안 기다리고 발생 즉시 기록).
    '화면 비우기' 지점 이후만 노출."""
    uid, err = _require_user()
    if err:
        return err
    limit = _int_arg('limit', 50)
    return jsonify({'ok': True, 'attempts': _db.list_submit_attempts(uid, limit=limit)})


@app.route('/api/m_submits/clear', methods=['POST'])
def api_m_submits_clear():
    """제출 시도 화면 비우기 — 데이터는 보존, 비우기 지점만 latest 로 이동."""
    uid, err = _require_user()
    if err:
        return err
    new_id = _db.set_last_cleared_submit_id(uid, _db.latest_submit_id(uid))
    return jsonify({'ok': True, 'last_cleared_submit_id': new_id})


@app.route('/api/m_status', methods=['GET'])
def api_m_status():
    """모바일 경량 상태 — 진행/완료 라운드, 오류 패턴 수, 제출 / 거절 알파 수."""
    uid, err = _require_user()
    if err:
        return err
    s = _db.get_user_status(uid)
    w = _worker.get(uid)
    return jsonify({
        'ok': True,
        'running': bool(s.get('running')),
        'paused': bool(s.get('paused')),
        'thread_alive': bool(w and w.is_alive()),
        'paused_in_memory': bool(w and w.is_paused()),
        'current_round': s.get('current_round'),
        'current_phase': int(s.get('current_phase') or 0),
        'current_round_label': s.get('current_round_label') or '—',
        'current_status': s.get('current_status') or '',
        'last_round_num': int(s.get('last_round_num') or 0),
        'errors_count': _db.total_errors_count(uid),
        'submitted_count': _db.submitted_count(uid),
        'unsubmitted_count': _db.unsubmitted_count(uid),
    })


# ─────────────────────────────────────────────────────────────────────────────
# 로그 + 라운드
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/logs', methods=['GET'])
def api_logs():
    uid, err = _require_user()
    if err:
        return err
    since = _int_arg('since', 0)
    limit = _int_arg('limit', 500)
    rows = _db.list_logs_since(uid, since_id=since, limit=limit)
    return jsonify({'ok': True, 'logs': rows})


@app.route('/api/logs/clear', methods=['POST'])
def api_logs_clear():
    """화면 비우기 — DB 의 로그 데이터는 그대로, 비우기 지점만 latest 로 이동.
    재접속/새로고침 시 이 지점 이후 로그만 replay 된다."""
    uid, err = _require_user()
    if err:
        return err
    new_id = _db.set_last_cleared_log_id(uid, _db.latest_log_id(uid))
    return jsonify({'ok': True, 'last_cleared_log_id': new_id})


@app.route('/api/logs/stream', methods=['GET'])
def api_logs_stream():
    """SSE 스트림 — 로그 한 줄 추가될 때마다 push (1초 폴링)."""
    uid = _current_user_id()
    if uid is None:
        return _err('unauthorized', '', 401)

    since = _int_arg('since', 0)

    def _gen():
        last_id = since
        # 첫 연결 직후 상태 한 번.
        yield f'event: status\ndata: {json.dumps(_status_payload(uid), ensure_ascii=False)}\n\n'
        # 무한 폴링.
        idle_ticks = 0
        while True:
            try:
                rows = _db.list_logs_since(uid, since_id=last_id, limit=200)
            except Exception as e:
                yield f'event: error\ndata: {json.dumps({"err": str(e)})}\n\n'
                time.sleep(2.0)
                continue
            if rows:
                for r in rows:
                    payload = json.dumps(r, ensure_ascii=False)
                    yield f'event: log\ndata: {payload}\n\n'
                last_id = rows[-1]['id']
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 10:  # 10초마다 keepalive + status.
                    yield f'event: status\ndata: {json.dumps(_status_payload(uid), ensure_ascii=False)}\n\n'
                    idle_ticks = 0
                else:
                    yield 'event: ping\ndata: {}\n\n'
            time.sleep(1.0)

    headers = {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',  # nginx/Cloudflare 버퍼링 끄기
        'Connection': 'keep-alive',
    }
    return Response(_gen(), headers=headers)


@app.route('/api/rounds', methods=['GET'])
def api_rounds():
    uid, err = _require_user()
    if err:
        return err
    limit = _int_arg('limit', 50)
    return jsonify({'ok': True, 'rounds': _db.list_rounds(uid, limit=limit)})


@app.route('/api/best', methods=['GET'])
def api_best():
    """Submitted 알파만 — WQB Submit 이 실제로 성공한 것."""
    uid, err = _require_user()
    if err:
        return err
    return jsonify({'ok': True, 'best': _db.list_submitted_alphas(uid, limit=50)})


@app.route('/api/recent_alphas', methods=['GET'])
def api_recent_alphas():
    uid, err = _require_user()
    if err:
        return err
    limit = _int_arg('limit', 60)
    return jsonify({'ok': True, 'alphas': _db.list_recent_alphas(uid, limit=limit)})


@app.route('/api/errors', methods=['GET'])
def api_errors():
    uid, err = _require_user()
    if err:
        return err
    return jsonify({'ok': True, 'errors': _db.list_error_patterns(uid, limit=100)})


# ─────────────────────────────────────────────────────────────────────────────
# 헬스 + 진단
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/health', methods=['GET'])
def api_health():
    return jsonify({'ok': True, 'ts': time.time()})


def main():
    # import 부작용 제거 → 시작 시 1회 명시 수행 (fail-fast + 테스트 가능).
    _db.init()
    _ensure_house_rc_account()
    interrupted = _db.interrupt_open_rounds()
    if interrupted:
        LOG.info('interrupted stale open rounds on startup: %d', interrupted)
    _auto_resume_workers()
    port = int(os.environ.get('HYFE_IQC_PORT', '8088'))
    # 코드 기본값도 안전하게 루프백 (run.sh/systemd 없이 직접 실행해도 공개망
    # 직접 노출 방지). LAN/공인 IP 공개는 HYFE_IQC_HOST=0.0.0.0 으로 명시.
    host = os.environ.get('HYFE_IQC_HOST', '127.0.0.1')
    debug = os.environ.get('HYFE_IQC_DEBUG', '').lower() in ('1', 'true', 'yes')
    LOG.info('starting HYFE_IQC on %s:%d (debug=%s)', host, port, debug)
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == '__main__':
    main()
