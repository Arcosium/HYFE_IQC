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
    """WQB 자격증명 + Gemini API 키 검증.

    기존 사용자 fast-path: DB 에 같은 username 이 있고 비밀번호가 일치하면 WQB 브라우저
    검증을 건너뛴다. Gemini 키는 입력값 우선, 무효/미입력이면 저장된 유효 키로 폴백
    하므로 새 기기에서는 ID+비밀번호만으로 로그인 가능 (둘 다 무효일 때만 거부). 이래야
    (a) 다른 기기에서 동시에 로그인할 때 chromium subprocess 충돌이 없고, (b) 워커가
    돌고 있는 와중에도 새 디바이스가 즉시 합류 가능. (worker 는 user_id 기준으로
    in-process 공유 — 두 기기가 같은 워커의 상태/로그를 같이 본다.)
    """
    body = request.get_json(silent=True) or {}
    wqb_username = (body.get('wqb_username') or '').strip()
    wqb_password = (body.get('wqb_password') or '').strip()
    gemini_api_key = (body.get('gemini_api_key') or '').strip()
    remember = bool(body.get('remember', True))

    if not (wqb_username and wqb_password):
        return _err('missing_fields', '아이디 / 비밀번호가 필요합니다', 400)

    # ── 기존 사용자 fast-path ──
    # 비밀번호만 맞으면 통과시키되, Gemini 키는 다음 우선순위로 확정:
    #   1) 입력한 키가 유효 → 그 키 채택(변경 시 DB 갱신)
    #   2) 입력 키가 없거나 무효지만 저장된 키가 유효 → 저장 키로 폴백
    #      → 새 기기에서 ID+비밀번호만으로 로그인 가능 (모바일 m_login 과 동일 철학)
    #   3) 입력·저장 둘 다 무효 → 새 키 발급 요구 (워커가 유효 키 없이는 못 돌기 때문)
    existing = _db.find_user_by_username(wqb_username)
    if existing and existing.get('wqb_password') == wqb_password:
        uid = int(existing['id'])
        stored_key = existing.get('gemini_api_key') or ''

        # 1) 입력한 키가 유효하면 채택 (변경됐으면 갱신).
        if gemini_api_key:
            g = _auth.validate_gemini_key(gemini_api_key)
            if g.get('ok'):
                LOG.info('login fast-path (existing, 입력 키 OK): %s', wqb_username)
                if gemini_api_key != stored_key:
                    _db.update_user_secrets(uid, gemini_api_key=gemini_api_key)
                else:
                    _db.update_user_secrets(uid)  # last_login_at touch
                return _issue_session(uid, wqb_username, remember)
            LOG.info('login fast-path (existing, 입력 키 무효:%s) — 저장 키로 폴백: %s',
                     g.get('reason'), wqb_username)

        # 2) 저장된 키로 폴백 (입력 키와 다를 때만 재검증 가치 있음).
        if stored_key and stored_key != gemini_api_key:
            gs = _auth.validate_gemini_key(stored_key)
            if gs.get('ok'):
                LOG.info('login fast-path (existing, 저장 키 폴백 OK): %s', wqb_username)
                _db.update_user_secrets(uid)  # last_login_at touch
                return _issue_session(uid, wqb_username, remember)

        # 3) 입력 키도 저장 키도 무효 → 새 키 필요.
        LOG.info('login fail (existing, 유효 Gemini 키 없음 — 입력·저장 모두 무효): %s',
                 wqb_username)
        return _err('gemini_invalid',
                    'Gemini API 키가 만료/무효합니다. 새 키를 발급받아 입력해 주세요 '
                    '(https://aistudio.google.com/app/api-keys).', 401)

    # ── 신규 사용자 (또는 비밀번호 mismatch) — 풀 검증 ──
    # 비밀번호 mismatch 인데 username 은 존재하는 케이스: 의도적으로 기존 record 를
    # 보호하기 위해 거부. WQB 비밀번호를 진짜 변경했다면 관리자가 DB record 를
    # 직접 정리해야 한다. (이렇게 해야 누군가 username 을 알아내고 비밀번호를 마구
    # 시도할 때 매번 chromium subprocess 가 뜨는 걸 방지.)
    if existing and existing.get('wqb_password') != wqb_password:
        LOG.info('login fail (password mismatch for existing user): %s', wqb_username)
        return _err('wqb_credentials',
                    '저장된 자격증명과 일치하지 않습니다. WQB 비밀번호를 변경했다면 '
                    '관리자에게 record 초기화를 요청하세요.', 401)

    # 여기 도달 = existing is None (신규 사용자). 신규 가입에는 Gemini 키 필수
    # (저장된 키가 없으니 폴백 대상이 없고, 워커가 유효 키 없이는 못 돈다).
    if not gemini_api_key:
        return _err('missing_fields',
                    '신규 가입에는 Gemini API 키가 필요합니다.', 400)

    # 신규 가입 — chromium subprocess 보호용 in-flight 락 (같은 username 이 동시에
    # 두 번 풀 검증을 트리거하지 않도록).
    with _LOGIN_LOCK_LOCK:
        if wqb_username in _LOGIN_IN_FLIGHT:
            return _err('login_in_progress',
                        '이미 같은 WQB 아이디로 검증이 진행 중입니다. 잠시 후 다시 시도해 주세요.',
                        429)
        _LOGIN_IN_FLIGHT.add(wqb_username)

    try:
        LOG.info('login attempt (full validation): %s', wqb_username)
        result = _auth.validate_login(wqb_username, wqb_password, gemini_api_key)
    finally:
        with _LOGIN_LOCK_LOCK:
            _LOGIN_IN_FLIGHT.discard(wqb_username)

    if not result.get('ok'):
        LOG.info('login fail %s: %s — %s',
                 wqb_username, result.get('reason'), result.get('detail'))
        return jsonify(result), 401

    uid = _db.upsert_user(wqb_username, wqb_password, gemini_api_key)
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
    token = _db.create_session(uid)
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


@app.route('/api/logout', methods=['POST'])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE) or ''
    if token:
        _db.delete_session(token)
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
    return jsonify({
        'ok': True,
        'user_id': uid,
        'wqb_username': u['wqb_username'],
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
