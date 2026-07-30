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

LOG = logging.getLogger('genomicwqb.app')
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


def _install_shutdown_handler() -> None:
    """SIGTERM/SIGINT 에 진행 중인 WQB 시뮬을 취소하고 종료한다.

    ⚠ 2026-07-28 실측. 안 하면 시뮬이 WQB 쪽에서 계속 돌며 동시 슬롯을 물고, 재시작
    직후 라운드가 빈 슬롯 1개로 시작한다 — 스레드 8개 중 7개가 t=0 부터 대기했고
    첫 결과가 30분 뒤에 나왔다(id5470 을 죽이고 시작한 id5471).

    워커에는 request_shutdown 을 쓴다(request_pause 가 아니다) — paused 플래그를
    남기면 재시작 후 자동 재개가 이 사용자를 건너뛴다.
    """
    import signal as _signal

    def _on_term(signum, _frame):
        try:
            n = _worker.shutdown_all()
            LOG.warning('signal %s — 시뮬 %d건 취소 후 종료', signum, n)
        except Exception as e:
            LOG.warning('종료 정리 실패(그대로 종료): %s', e)
        raise SystemExit(0)

    for _sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            _signal.signal(_sig, _on_term)
        except (ValueError, OSError) as e:      # 메인 스레드가 아니면 등록 불가
            LOG.warning('signal %s 핸들러 등록 skip: %s', _sig, e)


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
    s['submit_mode'] = _db.get_submit_mode(uid)
    # ⚠ 이 세 문자열은 2026-07-27 까지 옛 Playwright 시대 표현이 남아 사실과 달랐다
    #   ('Non-RC 는 브라우저로 돌고 제출 안 함'). 시뮬은 계정 종류와 무관하게 REST API
    #   단일이고(2026-07-13 Playwright 제거), 제출도 양쪽 다 시도한다. 실제 차이만 적는다.
    s['genome_model'] = 'rc-api-genome' if is_rc else 'standard-genome'
    s['backtester_mode'] = 'WQB API concurrent'
    # save_policy(파이프라인 '게이트' 줄에 붙던 정책 설명)는 2026-07-29 사장 지시로 삭제.
    # 소비처가 UI 한 곳뿐이었다.
    # GA(유전 알고리즘) 상태 — UI Evolution 패널이 소비.
    try:
        _seeds = _db.elite_seeds(uid, top_n=5)
    except Exception:
        _seeds = []
    seed_pool = len(_seeds)
    seed_generation = max(
        (int((s.get('genome') or {}).get('generation') or 0) for s in _seeds), default=0)
    try:
        focus_len = len(_db.get_focus_queue(uid))
    except Exception:
        focus_len = 0
    bandit_on = _run_config.is_bandit_enabled()
    s['ga'] = {'seed_pool': seed_pool, 'focus_queue': focus_len, 'bandit': bandit_on,
               'seed_generation': seed_generation}
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

    이미 등록된 username 이면 409.
    account_type 은 **묻지 않고 측정한다** — /authentication 이 주는 permissions 의
    CONSULTANT 표식으로 정한다(2026-07-27). 예전엔 가입 폼 라디오 버튼이 정했는데,
    자기 신고라 실제 WQB 권한과 어긋날 수 있었고 그걸 바로잡을 승급 검사도
    '로그인 되면 RC' 라 게이트 구실을 못 했다.
    Gemini API 키는 2026-07-03부터 받지 않는다 (Genome GA 전환으로 불필요).
    기존 사용자 로그인은 /api/login 을 사용할 것.
    """
    body = request.get_json(silent=True) or {}
    wqb_username = (body.get('wqb_username') or '').strip()
    wqb_password = (body.get('wqb_password') or '').strip()
    remember = bool(body.get('remember', True))

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
        result = _auth.validate_login(wqb_username, wqb_password)
    finally:
        with _LOGIN_LOCK_LOCK:
            _LOGIN_IN_FLIGHT.discard(wqb_username)

    if not result.get('ok'):
        LOG.info('register fail %s: %s — %s',
                 wqb_username, result.get('reason'), result.get('detail'))
        return jsonify(result), 401

    account_type = result.get('account_type') or 'standard'
    uid = _db.upsert_user(wqb_username, wqb_password, '', account_type=account_type)
    # 능력 탐침이 정한 백엔드를 저장 — 시뮬은 REST API 단일이다(Playwright 제거).
    if result.get('backend') == 'api':
        try:
            _db.set_backend(uid, 'api')
        except Exception:
            pass
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


@app.route('/api/account/upgrade-to-rc', methods=['POST'])
def api_upgrade_to_rc():
    """WQB 실제 권한을 다시 읽어 account_type 을 **동기화**한다 (승급·강등 양방향).

    ⚠ 예전엔 'WQB API 로그인 성공 = RC' 로 판정했는데, 일반 계정도 API Basic 인증이
    통과하므로(auth.probe_wqb_backend 주석) 로그인만 되면 누구나 RC 가 됐다 —
    게이트 구실을 못 했다. 이제 /authentication 이 주는 permissions 배열의
    CONSULTANT 표식으로 판정한다 (2026-07-27 실측).
    """
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
    at = _auth.account_type_for(v.get('permissions'))
    _db.set_account_type(uid, at)
    if at != 'research_consultant':
        return jsonify({'ok': False, 'reason': 'wqb_not_consultant',
                        'account_type': at,
                        'detail': 'WQB 계정에 CONSULTANT 권한이 없습니다. '
                                  'Research Consultant 승인 후 다시 시도하세요.'}), 400
    return jsonify({'ok': True, 'account_type': at})


@app.route('/api/account/wqb-persona-status', methods=['GET'])
def api_wqb_persona_status():
    """현재 사용자의 WQB 페르소나 완료 여부 확인.

    이 엔드포인트는 **읽기 전용**이다 — 저장된 세션과 .pending 파일만 본다.
    절대 passive 하게 POST /authentication 을 하지 않는다: 매 조회마다 POST 하면
    미완료 persona 상태에서 WQB 가 BIOMETRICS_THROTTLED(429)를 매번 재무장시켜
    throttle 가 영원히 안 풀린다(사장님 "버튼 안 눌림" 버그의 근본 원인).
    세션도 pending challenge 도 없을 때만 인증 1회로 신규 challenge 를 발급한다.

    ⚠ persona_url 은 **여기서 돌려주지 않는다**(항상 ''). 링크 해석은 inquiry 를 재개시켜
    사용자가 열어 둔 인증 페이지를 죽이므로, 사용자가 링크를 누르는 순간에만
    /api/account/wqb-persona-link 로 발급한다. 여기선 challenge 존재 여부만 알린다.
    """
    uid = _current_user_id()
    if not uid:
        return _err('not_logged_in', '로그인이 필요합니다', 401)
    creds = _db.get_user_credentials(uid)
    if not creds:
        return _err('no_credentials', '자격증명을 찾을 수 없습니다', 400)
    u, p, _ = creds
    account_type = _db.get_account_type(uid)
    # 1) 미완료 persona challenge(.pending)가 있으면 **세션이 아직 살아 있어도** 먼저 알린다.
    #    session_keeper 가 만료 30분 전 선제 갱신(refresh_token)에서 persona 를 요구받으면
    #    .pending 만 만들고 살아있는 세션은 그대로 두는데, 예전엔 '세션 유효 → 인증 불필요'
    #    fast-path 가 이를 가려서 — 30분 전 알림을 받고 들어와도 인증 버튼이 안 떴다
    #    (만료 전 선인증이 불가능했다). resolve=False — 파일만 읽는다(네트워크 0건).
    #    사용자가 브라우저에서 완료 후 '완료' 버튼을 누르면 그때 complete_persona()
    #    가 단 한 번 POST 한다. 여기서 POST 하면 throttle 재무장 루프가 된다.
    try:
        from .wqb_api import WqbApiClient
        c = WqbApiClient(u, p)
        pend = c.pending_persona(resolve=False)
        if pend is not None:
            authed = False
            try:
                authed = bool(c._load_session() and c._session_valid())
            except Exception:
                authed = False
            return jsonify({'persona_required': True,
                            'persona_url': '',
                            'inquiry': pend.get('inquiry', ''),
                            'authenticated': authed,
                            'account_type': account_type})
        # 1.5) pending 이 없고 저장된 세션이 살아있으면 biometric 불필요 — POST 없이 반환.
        if c._load_session() and c._session_valid():
            return jsonify({'persona_required': False, 'authenticated': True,
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
            pend = c.pending_persona(resolve=False) or {}
            return jsonify({'persona_required': True,
                            'persona_url': '',
                            'inquiry': pend.get('inquiry', ''),
                            'account_type': account_type})
        if getattr(c, 'last_auth_status_code', None) == 429:
            return jsonify({'persona_required': False, 'rate_limited': True,
                            'detail': 'WQB API 인증 호출 한도(분당 5회) 초과 — 1분 후 다시 시도하세요.',
                            'account_type': account_type})
    except Exception:
        pass
    return jsonify({'persona_required': False, 'ok': False,
                    'account_type': account_type})


@app.route('/api/account/wqb-persona-watch', methods=['GET'])
def api_wqb_persona_watch():
    """모바일 앱 폴링 전용 — 미완료 persona challenge(.pending)만 읽는다.

    /api/account/wqb-persona-status 를 그대로 폴링하면 안 된다: 저장 세션도 .pending 도
    없을 때 그 엔드포인트는 WQB 에 POST /authentication 을 날려 새 challenge 를 발급하는데,
    60초마다 그러면 BIOMETRICS_THROTTLED(429) 가 영구 재무장된다.

    여기서는 WQB 로 나가는 네트워크 호출이 **0건**이다 — 로컬 .pending 파일만 본다.
    'persona_required' 는 곧 "처리 대기 중인 바이오 인증 challenge 가 있다" 는 뜻이고,
    그게 정확히 앱이 알림을 띄워야 하는 시점이다.

    ⚠ persona_url 은 항상 '' 이다. 예전엔 여기서 링크를 해석해 돌려줬는데, 그 해석
    (`GET /authentication/persona?inquiry=…`) 이 inquiry 를 재개시켜 **직전 Persona 세션을
    무효화**한다. 60초마다 그러면 사용자가 인증 중인 페이지가 계속 죽어 무한 새로고침 끝에
    'session expired' 가 뜬다(사장 보고 2026-07-10). 앱은 URL 없는 알림을 받으면 앱을 열고,
    링크는 사용자가 누를 때 /api/account/wqb-persona-link 가 그 시점에 발급한다.
    """
    uid = _current_user_id()
    if not uid:
        return _err('not_logged_in', '로그인이 필요합니다', 401)
    creds = _db.get_user_credentials(uid)
    if not creds:
        return _err('no_credentials', '자격증명을 찾을 수 없습니다', 400)
    u, p, _ = creds
    try:
        from .wqb_api import WqbApiClient
        pend = WqbApiClient(u, p).pending_persona(resolve=False)
    except Exception:
        LOG.exception('persona-watch 조회 실패 uid=%s', uid)
        return jsonify({'persona_required': False, 'ok': False})
    if pend is None:
        return jsonify({'persona_required': False, 'ok': True})
    return jsonify({'persona_required': True, 'ok': True,
                    'persona_url': '',
                    'inquiry': pend.get('inquiry', '')})


@app.route('/api/account/wqb-persona-link', methods=['POST'])
def api_wqb_persona_link():
    """사용자가 '인증 페이지 열기' 를 누른 그 순간에만 브라우저용 Persona 링크를 발급한다.

    POST 인 이유: 조회가 아니라 **상태를 바꾸는 호출**이다. WQB 에 GET 하면 inquiry 가
    재개되며 새 hosted-flow 세션이 나오고 직전 세션은 무효가 된다. 그래서 폴링·페이지
    진입 경로에서는 절대 부르지 않는다.

    challenge 가 이미 죽었으면(WQB 410 Gone) pending_persona 가 .pending 을 지우므로,
    그 자리에서 authenticate() 로 새 challenge 를 한 번 발급해 링크를 돌려준다.

    body `{"force": true}` — 사용자가 **'인증 링크 재발급'** 버튼을 누른 경우.
    링크를 열었더니 Persona 가 'session expired' 를 띄우는 상황(hosted 세션이 이미
    죽었지만 WQB 쪽 challenge 는 살아 있어 자동 재발급 조건에 안 걸린다)은 사용자가
    스스로 빠져나올 방법이 없었다. force 면 저장된 challenge 를 **무조건 버리고**
    mint_challenge() 로 새로 발급한다. 대가: 직전 링크는 그 순간 무효가 되고
    POST /authentication 이 1회 나간다(429 throttle 이 있으니 자동 경로에서는 금지 —
    사용자가 명시적으로 누를 때만 이 플래그가 온다).
    """
    uid = _current_user_id()
    if not uid:
        return _err('not_logged_in', '로그인이 필요합니다', 401)
    creds = _db.get_user_credentials(uid)
    if not creds:
        return _err('no_credentials', '자격증명을 찾을 수 없습니다', 400)
    u, p, _ = creds
    force = bool((request.get_json(silent=True) or {}).get('force'))
    from .wqb_api import WqbApiClient, _is_public_persona_url
    try:
        c = WqbApiClient(u, p)
        # 선제 갱신 마커(source='refresh') — session_keeper 가 만료 30분 전에 만든
        # challenge 는 살아있는 세션 쿠키 아래에서 발급돼, 해석하면 이미 죽은 hosted
        # 세션이 나온다(열자마자 'session expired', 2026-07-16 사장 보고). 사용자가
        # 지금 눌렀으니 만료-후와 동일한 검증된 경로로 **그 자리에서** 새로 발급한다.
        _raw = getattr(c, '_read_pending', lambda: None)()
        if force or (_raw is not None and str(_raw.get('source') or '') == 'refresh'):
            if force:
                LOG.info('persona 링크 강제 재발급 요청 uid=%s', uid)
            if c.mint_challenge():
                return jsonify({'ok': True, 'authenticated': True, 'persona_required': False})
            if getattr(c, 'last_auth_status_code', None) == 429:
                return jsonify({'ok': False, 'reason': 'rate_limited', 'persona_required': True,
                                'detail': 'WQB 인증 호출 한도(분당 5회) 초과 — 1분 후 다시 눌러주세요.'})
            if not c.persona_required:
                return _err('persona_unavailable', 'WQB 인증에 실패했습니다', 400)
            # mint 성공 → 아래 pending_persona(resolve=True) 가 fresh challenge 를 해석한다.
        pend = c.pending_persona(resolve=True)
        if pend is None:
            # 죽은 challenge 였다(또는 애초에 없었다) → 새로 발급하고 다시 해석.
            if c.authenticate():
                return jsonify({'ok': True, 'authenticated': True, 'persona_required': False})
            if not c.persona_required:
                return _err('persona_unavailable', 'WQB 인증에 실패했습니다', 400)
            pend = c.pending_persona(resolve=True) or {}
        url = pend.get('persona_url') or ''
        if not _is_public_persona_url(url):
            # 일시 해석 실패 — challenge 는 살아 있다. 재발급하지 말고 재시도를 안내한다.
            return jsonify({'ok': False, 'reason': 'resolving', 'persona_required': True,
                            'detail': 'biometric 링크를 준비 중입니다 — 잠시 후 다시 눌러주세요.'})
        return jsonify({'ok': True, 'persona_required': True,
                        'persona_url': url, 'inquiry': pend.get('inquiry', '')})
    except Exception as e:
        LOG.exception('persona-link 발급 실패 uid=%s', uid)
        return _err('persona_failed', f'링크 발급 실패: {e}', 400)


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


# delay 는 이제 탐색 조건(/api/constraint)의 `delay=0|1` 이 정한다. 옛 /api/delay_mode
# 토글은 같은 값을 두 곳에서 정해(조건 vs 모드) 조건을 걸어도 다른 delay 로 도는 사고를
# 냈으므로 2026-07-22 제거했다.


@app.route('/api/constraint', methods=['GET', 'POST', 'DELETE'])
def api_constraint():
    """탐색 조건 조회/설정/해제.

    Power Pool 주간 테마처럼 **기한이 있는** 조건이 대부분이라, 걸기만큼 **끄기가
    중요하다**. 주가 바뀌면 즉시 풀 수 있어야 낡은 조건으로 라운드를 낭비하지 않는다.
    워커가 매 라운드 새로 읽으므로 재시작 없이 다음 라운드부터 반영된다.

    POST  {"text": "region=USA & delay=1 & …"}  또는 자연어
    DELETE (또는 POST 에 빈 text) → 해제
    """
    uid, err = _require_user()
    if err:
        return err

    def _payload():
        raw = _run_config.get_constraint_text()
        spec = _run_config.get_constraint()
        return {'ok': True, 'text': raw,
                'active': spec is not None,
                'summary': spec.describe() if spec else '',
                'unparsed': list(spec.unparsed) if spec else []}

    if request.method == 'GET':
        return jsonify(_payload())
    if request.method == 'DELETE':
        _run_config.set_constraint_text('')
        return jsonify(_payload())

    text = str((request.get_json(silent=True) or {}).get('text', '') or '').strip()
    if not text:
        _run_config.set_constraint_text('')
        return jsonify(_payload())
    from . import constraint_spec as _cspec
    spec = _cspec.parse(text)
    if spec.is_empty():
        # 아무것도 해석 못 한 조건을 저장하면 '걸었다고 믿는데 실제로는 무제약' 이 된다.
        return _err('bad_constraint',
                    '조건을 하나도 해석하지 못했습니다. 예: '
                    "region=USA & delay=1 & universe=TOP1000 & datasets not in ['pv1']", 400)
    _run_config.set_constraint_text(text)
    return jsonify(_payload())


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
    tail = _int_arg('tail', 0)
    if tail > 0:
        # 초기 로딩 — 마지막 n 줄만 (비우기 지점 존중). backlog 전체 재생 금지.
        return jsonify({'ok': True,
                        'logs': _db.list_logs_tail(uid, n=min(tail, 5000))})
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


@app.route('/api/alpha', methods=['GET'])
def api_alpha():
    """알파 1건 상세 — pk 또는 code 로. **본인 것만**.

    리더보드는 /api/recent_alphas 에 실린 행을 그대로 쓰지만, 제출 내역·제출 대기는
    그 목록(최근 N건) 밖의 알파를 가리킬 수 있어서 따로 조회한다.
    """
    uid, err = _require_user()
    if err:
        return err
    pk = _int_arg('pk', 0)
    a = _db.get_alpha_by_id(uid, pk) if pk > 0 else None
    if a is None:
        a = _db.get_alpha_by_code(uid, (request.args.get('code') or '').strip())
    if a is None:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    return jsonify({'ok': True, 'alpha': a})


@app.route('/api/errors', methods=['GET'])
def api_errors():
    uid, err = _require_user()
    if err:
        return err
    return jsonify({'ok': True, 'errors': _db.list_error_patterns(uid, limit=100)})


@app.route('/api/research', methods=['POST'])
def api_research():
    """전략 리서치 요청 — 웹 근거 수집 → LLM 가설 → 타입드 유전체 후보 생성.

    산출물(strategy_specs)은 워커가 다음 라운드에 초기 개체로 소비한다.
    요청을 넣지 않으면 워커는 지금처럼 무작위 GA 를 돈다(이 엔드포인트는 선택 사항).
    """
    uid, err = _require_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    query = str(data.get('query') or '').strip()
    if not query:
        return _err('bad_request', '요청 내용이 비어 있습니다', 400)
    if len(query) > 500:
        return _err('bad_request', '요청은 500자 이내로 적어주세요', 400)
    from . import pipeline as _pipeline
    try:
        run_id = _pipeline.start(uid, query)
    except RuntimeError as e:
        return _err('busy', str(e), 409)
    except ValueError as e:
        return _err('bad_request', str(e), 400)
    return jsonify({'ok': True, 'run_id': run_id})


@app.route('/api/research/status', methods=['GET'])
def api_research_status():
    """최근(또는 지정) 리서치 런의 진행 상황 + 가설 + 전략 후보."""
    uid, err = _require_user()
    if err:
        return err
    from . import pipeline as _pipeline
    run_id = request.args.get('run_id', type=int)
    run = _db.get_research_run(run_id) if run_id else _db.latest_research_run(uid)
    if not run or int(run.get('user_id') or 0) != uid:
        return jsonify({'ok': True, 'run': None, 'running': _pipeline.is_running(uid),
                        'specs': _db.spec_counts(uid)})
    hypos = _db.list_hypotheses(int(run['id']))
    specs = _db.list_specs_for_run(int(run['id']))
    by_hypo: dict[int, list] = {}
    for s in specs:
        by_hypo.setdefault(int(s['hypothesis_id']), []).append({
            'id': s['id'], 'code': s['code'], 'status': s['status'],
            'why': s['why'], 'delay': s['delay'], 'genome': s['genome'],
            'alpha_id': s['alpha_id'],
        })
    for h in hypos:
        h['specs'] = by_hypo.get(int(h['id']), [])
    run.pop('evidence', None)   # 근거 원문은 수만 자 — 목록 응답에 싣지 않는다
    return jsonify({
        'ok': True,
        'run': run,
        'hypotheses': hypos,
        'running': _pipeline.is_running(uid),
        'specs': _db.spec_counts(uid),
    })


@app.route('/api/learning', methods=['GET'])
def api_learning():
    """온라인 학습 현황 — '어떤 조정이 어떤 지표를 나아지게 했나' 대시보드 데이터.

    directives: (부모 fail category × 적용 변이 축) 성공률 행렬 (귀속 엣지 집계).
    axes/operators: 최근 축·연산자별 효과 리더보드 (db.axis_effectiveness 등).
    bandit: arm 별 평균 보상·방문수 (settings 3종 + family/combine).
    """
    uid, err = _require_user()
    if err:
        return err
    axes = {ax: _db.axis_effectiveness(uid, ax)
            for ax in ('universe', 'neutralization', 'decay')}
    operators = _db.operator_effectiveness(uid)
    directives = [
        {'category': c, 'directive': d,
         'n': v['n'], 'wins': v['wins'], 'win_rate': v['win_rate']}
        for (c, d), v in sorted(_db.directive_stats(uid).items())
    ]
    return jsonify({
        'ok': True,
        'axes': axes,
        'operators': operators,
        'directives': directives,
        'bandit': _db.bandit_stats(uid),
    })


# ─────────────────────────────────────────────────────────────────────────────
# 제출 대기 큐 (2026-07-27) — 테마 미충족 보관(수동 재제출) + 예산 초과 대기
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/submit_mode', methods=['GET', 'POST'])
def api_submit_mode():
    """제출 모드 조회/변경 — 'auto'(자동 제출) | 'list'(대기 목록에만 추가).

    사용자별 설정이다(users.submit_mode). 제출은 되돌릴 수 없고 일일 예산을 쓰므로,
    남의 계정이 자기 뜻과 다르게 실제 제출을 내는 일이 없어야 한다.
    """
    uid, err = _require_user()
    if err:
        return err
    if request.method == 'GET':
        return jsonify({'ok': True, 'submit_mode': _db.get_submit_mode(uid)})
    mode = ((request.get_json(silent=True) or {}).get('submit_mode') or '').strip()
    if mode not in _db.SUBMIT_MODES:
        return _err('bad_mode', f'submit_mode 는 {" | ".join(_db.SUBMIT_MODES)} 중 하나여야 합니다', 400)
    return jsonify({'ok': True, 'submit_mode': _db.set_submit_mode(uid, mode)})


@app.route('/api/submit_queue', methods=['GET'])
def api_submit_queue():
    uid, err = _require_user()
    if err:
        return err
    return jsonify({'ok': True, 'rows': _db.submit_queue_list(uid)})


@app.route('/api/submit_queue/add', methods=['POST'])
def api_submit_queue_add():
    """리더보드 상세에서 알파 1건을 대기 목록에 넣는다 (kind='manual').

    WQB alpha id 는 클라이언트가 준 값을 쓰지 않고 서버가 DB 에서 다시 읽는다 —
    그대로 믿으면 남의 알파를 자기 큐에 넣을 수 있다.
    """
    uid, err = _require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    a = _db.get_alpha_by_id(uid, int(body.get('alpha_pk') or 0))
    if not a:
        return _err('not_found', '알파를 찾을 수 없습니다', 404)
    wid = str((a.get('metrics') or {}).get('wqb_alpha_id') or '')
    if not wid:
        return _err('no_wqb_id', 'WQB alpha id 가 없는 알파입니다 (시뮬 실패/캐시)', 400)
    added = _db.submit_queue_add(uid, wqb_alpha_id=wid, kind='manual',
                                 code=a.get('code') or '', alpha_pk=int(a['id']),
                                 note='리더보드에서 수동 추가',
                                 metrics=dict(a.get('metrics') or {}))
    status = 'pending'
    if not added:
        # 이미 있는 행. skipped 로 내려가 있으면 '추가' 를 누른 뜻대로 되살린다.
        # submitted 는 건드리지 않는다 — 끝난 것을 되돌리면 중복 제출이 된다.
        row = next((r for r in _db.submit_queue_list(uid, limit=500, include_skipped=True)
                    if r['wqb_alpha_id'] == wid and r['kind'] == 'manual'), None)
        status = (row or {}).get('status') or 'pending'
        if row and status == 'skipped':
            _db.submit_queue_mark(int(row['id']), 'pending', '리더보드에서 다시 추가')
            added, status = True, 'pending'
    return jsonify({'ok': True, 'added': added, 'status': status, 'wqb_alpha_id': wid})


@app.route('/api/submit_queue/delete', methods=['POST'])
def api_submit_queue_delete():
    """대기 큐에서 선택한 항목 삭제 (2026-07-28 사장 지시).

    'submitting' 인 행은 지우지 않는다 — 제출이 진행 중인데 행을 없애면 결과를
    기록할 자리가 사라진다.
    """
    uid, err = _require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    ids = body.get('ids')
    if ids is None and body.get('id') is not None:
        ids = [body.get('id')]
    if not isinstance(ids, list) or not ids:
        return _err('bad_ids', '삭제할 id 목록이 필요합니다', 400)
    busy = [int(r['id']) for r in _db.submit_queue_list(uid, limit=500,
                                                        include_skipped=True)
            if r['status'] == 'submitting']
    targets = [i for i in ids if int(i) not in busy]
    return jsonify({'ok': True, 'deleted': _db.submit_queue_delete(uid, targets),
                    'skipped_busy': len(ids) - len(targets)})


@app.route('/api/submit_queue/submit', methods=['POST'])
def api_submit_queue_submit():
    """대기 큐 1건 수동 제출 — 백그라운드 스레드로 시도하고 상태를 갱신한다.

    사전 체크 없이 곧장 제출을 시도한다(거절은 예산 미소모 — 테마가 바뀌었는지는
    WQB 의 판정이 진실이므로 미리 재지 않는다). UI 는 목록 폴링으로 결과를 본다.
    """
    uid, err = _require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    row = _db.submit_queue_get(int(body.get('id') or 0))
    if not row or row['user_id'] != uid:
        return _err('not_found', '큐 항목이 없습니다', 404)
    if row['status'] not in ('pending', 'rejected'):
        return _err('bad_state', f"이미 {row['status']} 상태입니다")
    creds = _db.get_user_credentials(uid)
    if not creds:
        return _err('no_creds', '자격증명이 없습니다', 400)
    _db.submit_queue_mark(row['id'], 'submitting', '수동 제출 진행 중…')

    def _do(qid: int, wid: str, code: str, alpha_pk, username: str, password: str):
        from . import wqb_api as _wqb_api
        try:
            client = _wqb_api.WqbApiClient(username, password)
            if not client.authenticate():
                _db.submit_queue_mark(qid, 'pending', 'WQB 인증 실패 — 재시도 가능')
                return
            try:
                from . import alpha_description as _adesc
                if code:
                    client.set_alpha_description(
                        wid, _adesc.build(code, genome=None, settings={}))
            except Exception:
                pass
            ok, st = client.submit_alpha(wid, deadline_s=600)
            _db.submit_queue_mark(qid, 'submitted' if ok else 'rejected', st[:200])
            _db.set_alpha_submit_result(int(alpha_pk or 0), ok, st,
                                        user_id=uid, code=code)
            _db.record_submit_attempt(uid, 0, 0, code or wid, ok, f'[manual-queue] {st}')
        except Exception as e:
            _db.submit_queue_mark(qid, 'pending', f'예외: {str(e)[:150]} — 재시도 가능')

    threading.Thread(
        target=_do,
        args=(row['id'], row['wqb_alpha_id'], row.get('code') or '',
              row.get('alpha_pk'), creds[0], creds[1]),
        daemon=True, name=f'queue-submit-{row["id"]}').start()
    return jsonify({'ok': True, 'id': row['id'], 'status': 'submitting'})


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ 슈퍼알파 (AAF SuperAlpha 이식) — env 게이트, 자동 제출 없음
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/superalpha/run', methods=['POST'])
def api_superalpha_run():
    """OS 알파 풀 위의 슈퍼알파 리서치 1런을 백그라운드로 시작한다.

    게이트: IQC_SUPERALPHA=1 + RC 계정. body(옵션): {seed_plus, n}.
    결과는 /api/superalpha/runs 로 조회 — 제출은 하지 않는다.
    """
    uid, err = _require_user()
    if err:
        return err
    if os.environ.get('IQC_SUPERALPHA', '0') != '1':
        return _err('disabled', 'IQC_SUPERALPHA=1 로 켜야 합니다', 403)
    if _db.get_account_type(uid) != 'research_consultant':
        return _err('not_rc', '슈퍼알파는 RC 계정 전용입니다', 403)
    creds = _db.get_user_credentials(uid)
    if not creds:
        return _err('no_creds', '자격증명이 없습니다', 400)
    body = request.get_json(silent=True) or {}
    try:
        seed_plus = int(body.get('seed_plus', 10))
        n = max(1, min(int(body.get('n', 6)), 15))
    except (TypeError, ValueError):
        return _err('bad_request', 'seed_plus/n 은 정수여야 합니다')
    from . import superalpha as _superalpha
    _superalpha.start_background(uid, creds[0], creds[1],
                                 seed_plus=seed_plus, n=n)
    return jsonify({'ok': True, 'seed_plus': seed_plus, 'n': n})


@app.route('/api/superalpha/runs', methods=['GET'])
def api_superalpha_runs():
    uid, err = _require_user()
    if err:
        return err
    return jsonify({'ok': True, 'runs': _db.superalpha_runs_list(uid)})


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
    # 세션 keep-alive — API 백엔드 계정의 토큰을 만료 전에 갱신해 얼굴 인증을 없앤다.
    try:
        from . import session_keeper
        session_keeper.start()
    except Exception as e:
        LOG.warning('session keeper 시작 실패(무시): %s', e)
    _install_shutdown_handler()
    port = int(os.environ.get('HYFE_IQC_PORT', '8088'))
    # 코드 기본값도 안전하게 루프백 (run.sh/systemd 없이 직접 실행해도 공개망
    # 직접 노출 방지). LAN/공인 IP 공개는 HYFE_IQC_HOST=0.0.0.0 으로 명시.
    host = os.environ.get('HYFE_IQC_HOST', '127.0.0.1')
    debug = os.environ.get('HYFE_IQC_DEBUG', '').lower() in ('1', 'true', 'yes')
    LOG.info('starting HYFE_IQC on %s:%d (debug=%s)', host, port, debug)
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == '__main__':
    main()
