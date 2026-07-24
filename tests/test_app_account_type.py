# tests/test_app_account_type.py
"""Task 9: account_type signup + RC upgrade endpoint tests.

Run: python3.11 -m pytest tests/test_app_account_type.py -v
"""
import json
import pytest

import server.app as app_mod
from flask import jsonify


@pytest.fixture
def client():
    app_mod.app.config['TESTING'] = True
    with app_mod.app.test_client() as c:
        yield c


# ── Test 1: Signup passes account_type to validate_login and upsert_user ────

def test_signup_passes_account_type(client, monkeypatch):
    """신규 가입 시 account_type='research_consultant' 가 validate_login 과
    upsert_user 에 정확히 전달되어야 한다. (가입 경로는 /api/register)"""
    captured = {}

    # 신규 사용자 — 찾을 수 없음
    monkeypatch.setattr(app_mod._db, 'find_user_by_username', lambda u: None)

    def fake_validate_login(u, p, g='', account_type='standard'):
        captured['validate_account_type'] = account_type
        return {'ok': True, 'reason': 'ok'}

    def fake_upsert_user(u, p, g, account_type='standard'):
        captured['upsert_account_type'] = account_type
        return 42  # fake uid

    def fake_issue_session(uid, wqb_username, remember):
        with app_mod.app.app_context():
            return jsonify({'ok': True, 'user_id': uid, 'wqb_username': wqb_username})

    monkeypatch.setattr(app_mod._auth, 'validate_login', fake_validate_login)
    monkeypatch.setattr(app_mod._db, 'upsert_user', fake_upsert_user)
    monkeypatch.setattr(app_mod, '_issue_session', fake_issue_session)

    r = client.post('/api/register', data=json.dumps({
        'wqb_username': 'user@example.com',
        'wqb_password': 'pass123',
        'gemini_api_key': 'AIzaSytest',
        'account_type': 'research_consultant',
    }), content_type='application/json')

    assert r.status_code == 200
    assert captured.get('validate_account_type') == 'research_consultant', \
        f"validate_login 에 account_type 미전달: {captured}"
    assert captured.get('upsert_account_type') == 'research_consultant', \
        f"upsert_user 에 account_type 미전달: {captured}"


# ── Test 1b–1e: login/register split (Task 1) ────────────────────────────────

def _client():
    return app_mod.app.test_client()


def test_login_rejects_unregistered(monkeypatch):
    def _no_validate_login(*a, **kw):
        raise AssertionError("validate_login must never be called from /api/login")
    monkeypatch.setattr(app_mod._auth, 'validate_login', _no_validate_login)
    monkeypatch.setattr(app_mod._db, 'find_user_by_username', lambda u: None)
    r = _client().post('/api/login', json={'wqb_username': 'new@x.com', 'wqb_password': 'pw'})
    assert r.status_code == 404 and r.get_json()['reason'] == 'not_registered'


def test_login_existing_password_match(monkeypatch):
    def _no_validate_login(*a, **kw):
        raise AssertionError("validate_login must never be called from /api/login")
    monkeypatch.setattr(app_mod._auth, 'validate_login', _no_validate_login)
    monkeypatch.setattr(app_mod._db, 'find_user_by_username',
                        lambda u: {'id': 2, 'wqb_password': 'pw', 'gemini_api_key': 'gk'})
    monkeypatch.setattr(app_mod._auth, 'validate_gemini_key', lambda k: {'ok': True})
    monkeypatch.setattr(app_mod._db, 'update_user_secrets', lambda *a, **k: None)
    monkeypatch.setattr(app_mod, '_issue_session',
                        lambda uid, u, r: app_mod.jsonify({'ok': True, 'user_id': uid}))
    r = _client().post('/api/login', json={'wqb_username': 'e', 'wqb_password': 'pw', 'gemini_api_key': 'gk'})
    assert r.get_json().get('ok') is True


def test_register_rejects_existing(monkeypatch):
    monkeypatch.setattr(app_mod._db, 'find_user_by_username', lambda u: {'id': 2, 'wqb_password': 'pw'})
    r = _client().post('/api/register', json={
        'wqb_username': 'e', 'wqb_password': 'pw',
        'gemini_api_key': 'gk', 'account_type': 'research_consultant',
    })
    assert r.status_code == 409 and r.get_json()['reason'] == 'already_registered'


def test_register_creates_with_account_type(monkeypatch):
    monkeypatch.setattr(app_mod._db, 'find_user_by_username', lambda u: None)
    captured = {}

    def fake_validate(u, p, g='', account_type='standard'):
        captured['vl'] = account_type
        return {'ok': True, 'reason': 'ok'}

    def fake_upsert(u, p, g, account_type='standard'):
        captured['up'] = account_type
        return 7

    monkeypatch.setattr(app_mod._auth, 'validate_login', fake_validate)
    monkeypatch.setattr(app_mod._db, 'upsert_user', fake_upsert)
    monkeypatch.setattr(app_mod, '_issue_session',
                        lambda uid, u, r: app_mod.jsonify({'ok': True}))
    r = _client().post('/api/register', json={
        'wqb_username': 'new@x.com', 'wqb_password': 'pw',
        'gemini_api_key': 'gk', 'account_type': 'research_consultant',
    })
    assert r.get_json().get('ok') is True
    assert captured['vl'] == 'research_consultant' and captured['up'] == 'research_consultant'


# ── Test 2: Upgrade success ──────────────────────────────────────────────────

def test_upgrade_to_rc_success(client, monkeypatch):
    """로그인된 사용자가 WQB API 검증 통과 시 RC로 전환되어야 한다."""
    captured = {}

    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 7)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('user@ex.com', 'pw', 'gemkey'))
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api', lambda u, p: {'ok': True, 'reason': 'ok'})

    def fake_set_account_type(uid, account_type):
        captured['uid'] = uid
        captured['account_type'] = account_type

    monkeypatch.setattr(app_mod._db, 'set_account_type', fake_set_account_type)

    r = client.post('/api/account/upgrade-to-rc', content_type='application/json')
    data = r.get_json()

    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {data}"
    assert data.get('ok') is True, f"Response not ok: {data}"
    assert captured.get('uid') == 7, f"set_account_type called with wrong uid: {captured}"
    assert captured.get('account_type') == 'research_consultant', \
        f"set_account_type called with wrong type: {captured}"


# ── Test 3: Upgrade rejected when WQB API says not consultant ───────────────

def test_upgrade_to_rc_rejected(client, monkeypatch):
    """WQB API 가 403 (not_consultant) 반환 시 400 응답, set_account_type 미호출."""
    set_called = []

    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 5)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('u@ex.com', 'pw', 'k'))
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api',
                        lambda u, p: {'ok': False, 'reason': 'wqb_not_consultant', 'detail': 'Not a consultant'})
    monkeypatch.setattr(app_mod._db, 'set_account_type', lambda uid, t: set_called.append((uid, t)))

    r = client.post('/api/account/upgrade-to-rc', content_type='application/json')
    data = r.get_json()

    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {data}"
    assert data.get('ok') is False or data.get('reason') == 'wqb_not_consultant', \
        f"Unexpected response: {data}"
    assert len(set_called) == 0, f"set_account_type should NOT have been called, but was: {set_called}"


# ── Task 5: Persona status / complete endpoints ──────────────────────────────

def _fake_wqb_client_no_session():
    """WqbApiClient stub: _load_session() returns False so flow reaches validate_wqb_api."""
    import server.wqb_api as wqb_api
    class FakeCli:
        def __init__(self, *a, **k):
            self.persona_required = False
            self.persona_url = None
            self.last_auth_status_code = None
        def _load_session(self): return False
        def _session_valid(self): return False
        def pending_persona(self, resolve=True):
            if not self.persona_required:
                return None
            assert resolve is False, 'status 는 링크를 해석하면 안 된다 (inquiry 재개 → 세션 무효화)'
            return {'persona_url': '', 'inquiry': 'inq_Z'}
        def authenticate(self):
            self.persona_required = True
            self.persona_url = 'https://api.worldquantbrain.com/authentication/persona?inquiry=inq_Z'
            return False
        def complete_persona(self, inquiry=None): return True
    return wqb_api, FakeCli


def test_persona_status_and_complete(monkeypatch):
    import server.app as app_mod
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))
    monkeypatch.setattr(app_mod._db, 'get_account_type', lambda uid: 'research_consultant')
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api',
        lambda u, p: (_ for _ in ()).throw(AssertionError('status should use WqbApiClient to persist persona cookies')))
    # Ensure WqbApiClient._load_session() returns False so flow falls through to validate_wqb_api
    wqb_api, FakeCli = _fake_wqb_client_no_session()
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCli)
    cl = app_mod.app.test_client()
    r = cl.get('/api/account/wqb-persona-status')
    j = r.get_json()
    assert j['persona_required'] is True
    # 링크는 여기서 주지 않는다 — /wqb-persona-link 가 사용자 클릭 시점에 발급한다.
    assert j['persona_url'] == ''
    # status response must include the raw inquiry
    assert j.get('inquiry') == 'inq_Z'
    # complete: FakeCli must accept the inquiry kwarg passed by the endpoint
    captured = {}
    class FakeCli2:
        def __init__(self, *a, **k): pass
        def _load_session(self): return False
        def _session_valid(self): return False
        def complete_persona(self, inquiry=None):
            captured['inquiry'] = inquiry
            return True
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCli2)
    r2 = cl.post('/api/account/wqb-persona-complete',
                 data=json.dumps({'inquiry': 'inq_Z'}),
                 content_type='application/json')
    assert r2.get_json()['ok'] is True
    assert captured.get('inquiry') == 'inq_Z'


def test_persona_status_pending_reports_challenge_without_post_or_resolve(monkeypatch):
    """저장된 세션이 만료됐지만 미완료 persona challenge(.pending)가 있으면,
    상태조회는 challenge 존재만 알리고 (a) validate_wqb_api(POST /authentication) 도,
    (b) 링크 해석(resolve=True) 도 하지 않아야 한다.

    (a) 는 biometric throttle 재무장 방지. (b) 는 사장 보고 2026-07-10 의 근본 원인:
    링크 해석은 inquiry 를 재개시켜 사용자가 열어 둔 Persona 페이지를 무효화한다."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))
    monkeypatch.setattr(app_mod._db, 'get_account_type', lambda uid: 'research_consultant')

    def _must_not_call(*a, **k):
        raise AssertionError('validate_wqb_api MUST NOT be called when a pending persona exists')
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api', _must_not_call)

    class FakeCliPending:
        def __init__(self, *a, **k): pass
        def _load_session(self): return True       # session file present...
        def _session_valid(self): return False     # ...but expired
        def pending_persona(self, resolve=True):
            assert resolve is False, 'status 는 링크를 해석하면 안 된다'
            return {'persona_url': '', 'inquiry': 'inq_P'}
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliPending)

    cl = app_mod.app.test_client()
    r = cl.get('/api/account/wqb-persona-status')
    j = r.get_json()
    assert j.get('persona_required') is True, j
    assert j.get('persona_url') == '', j
    assert j.get('inquiry') == 'inq_P', j
    assert j.get('rate_limited') is not True, j
    assert j.get('account_type') == 'research_consultant', j


def test_persona_status_pending_wins_over_valid_session(monkeypatch):
    """세션이 **아직 유효해도** 미완료 persona challenge(.pending)가 있으면
    persona_required=True 를 돌려줘야 한다.

    session_keeper 는 만료 30분 전 선제 갱신(refresh_token)에서 persona 를 요구받으면
    .pending 만 만들고 살아있는 세션은 그대로 둔다. 예전엔 '세션 유효 → 인증 불필요'
    fast-path 가 pending 확인보다 앞이라, 앱이 30분 전 알림을 보냈는데 사용자가 들어와도
    인증 버튼이 안 떴다(만료 전 선인증 불가 — 사장 보고 2026-07-14). authenticated=True
    를 함께 실어 UI 가 '지금 인증하면 끊김 없음' 안내를 띄운다."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))
    monkeypatch.setattr(app_mod._db, 'get_account_type', lambda uid: 'research_consultant')

    def _must_not_call(*a, **k):
        raise AssertionError('validate_wqb_api MUST NOT be called when a pending persona exists')
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api', _must_not_call)

    class FakeCliPendingLive:
        def __init__(self, *a, **k): pass
        def _load_session(self): return True       # 저장 세션 있음...
        def _session_valid(self): return True      # ...그리고 아직 살아 있음 (선제 갱신 창)
        def pending_persona(self, resolve=True):
            assert resolve is False, 'status 는 링크를 해석하면 안 된다'
            return {'persona_url': '', 'inquiry': 'inq_L'}
        def authenticate(self):
            raise AssertionError('pending 이 있으면 authenticate() 를 부르면 안 된다')
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliPendingLive)

    cl = app_mod.app.test_client()
    r = cl.get('/api/account/wqb-persona-status')
    j = r.get_json()
    assert j.get('persona_required') is True, j
    assert j.get('authenticated') is True, j
    assert j.get('persona_url') == '', j
    assert j.get('inquiry') == 'inq_L', j


def test_persona_link_remints_refresh_marker_on_the_spot(monkeypatch):
    """선제갱신 마커(source='refresh') pending 은 해석하면 죽은 hosted 세션이 나온다
    ('session expired', 2026-07-16 사장 보고). 링크 발급 엔드포인트는 마커를 보면
    그 자리에서 mint_challenge() 로 깨끗한 challenge 를 새로 발급한 뒤 해석해야 한다."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))

    calls = []

    class FakeCliMarker:
        persona_required = False
        def __init__(self, *a, **k): pass
        def _read_pending(self):
            # mint 전에는 마커, mint 후에는 fresh pending 이 보인다.
            return ({'persona_url': 'https://api/persona?inquiry=OLD', 'cookies': {},
                     'source': 'refresh'} if 'mint' not in calls
                    else {'persona_url': 'https://api/persona?inquiry=FRESH', 'cookies': {'c': '1'}})
        def mint_challenge(self):
            calls.append('mint')
            self.persona_required = True
            return False
        def authenticate(self):
            raise AssertionError('마커 경로는 authenticate() 가 아니라 mint_challenge() 를 타야 한다'
                                 ' (세션이 살아 있으면 authenticate 는 발급 없이 True 를 준다)')
        def pending_persona(self, resolve=True):
            calls.append('resolve')
            assert 'mint' in calls, 'mint 전에 마커를 해석하면 죽은 세션이 나온다'
            return {'persona_url': 'https://inquiry.withpersona.com/verify?x=1',
                    'inquiry': 'FRESH'}
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliMarker)

    cl = app_mod.app.test_client()
    r = cl.post('/api/account/wqb-persona-link')
    j = r.get_json()
    assert calls == ['mint', 'resolve'], calls
    assert j.get('ok') is True, j
    assert 'withpersona.com' in (j.get('persona_url') or ''), j


def test_persona_link_marker_mint_can_short_circuit_to_authenticated(monkeypatch):
    """마커 재발급 도중 WQB 가 '이미 인증됨'(mint True)이라면 그대로 성공 반환."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))

    class FakeCliAuthed:
        def __init__(self, *a, **k): pass
        def _read_pending(self):
            return {'persona_url': 'https://api/persona?inquiry=OLD', 'cookies': {},
                    'source': 'refresh'}
        def mint_challenge(self):
            return True
        def pending_persona(self, resolve=True):
            raise AssertionError('인증됐으면 해석할 것이 없다')
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliAuthed)

    cl = app_mod.app.test_client()
    j = cl.post('/api/account/wqb-persona-link').get_json()
    assert j.get('ok') is True and j.get('authenticated') is True, j


def test_persona_watch_is_passive_and_never_authenticates(monkeypatch):
    """모바일 앱이 60초마다 폴링하는 감시 엔드포인트는 WQB 로 절대 나가면 안 된다.

    두 가지를 동시에 못박는다:
      1. authenticate()/validate_wqb_api 를 부르면 BIOMETRICS_THROTTLED(429) 영구 재무장.
      2. pending_persona(resolve=True) 를 부르면 `GET /authentication/persona?inquiry=…` 가
         나가 inquiry 가 재개되고, 사용자가 열어 둔 Persona 페이지의 세션이 무효화된다.
         60초마다 그러면 인증 페이지가 무한 새로고침되다 'session expired' 로 끝난다
         (사장 보고 2026-07-10). 반드시 resolve=False 로 .pending 파일만 읽어야 한다.
    """
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('watch MUST NOT authenticate')))

    class FakeCliPending:
        def __init__(self, *a, **k): pass
        def authenticate(self):
            raise AssertionError('watch MUST NOT call authenticate()')
        def _session_valid(self):
            raise AssertionError('watch MUST NOT touch the WQB network')
        def pending_persona(self, resolve=True):
            assert resolve is False, 'watch MUST NOT resolve the persona link'
            return {'persona_url': '', 'inquiry': 'inq_W'}
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliPending)

    cl = app_mod.app.test_client()
    j = cl.get('/api/account/wqb-persona-watch').get_json()
    assert j['persona_required'] is True, j
    assert j['inquiry'] == 'inq_W', j
    assert j['persona_url'] == '', j   # 링크는 앱에 내려주지 않는다 — 알림은 앱을 열게 한다

    # pending 이 없으면 인증이 필요 없다고 보고한다 (역시 네트워크 호출 없음).
    class FakeCliClean(FakeCliPending):
        def pending_persona(self, resolve=True): return None
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliClean)
    j2 = cl.get('/api/account/wqb-persona-watch').get_json()
    assert j2['persona_required'] is False and j2['ok'] is True, j2


# ── /api/account/wqb-persona-link: 사용자 클릭 시점에만 링크를 발급 ──────────

def test_persona_link_resolves_live_challenge(monkeypatch):
    """살아있는 challenge 가 있으면 그 자리에서 해석해 브라우저용 URL 을 준다."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))

    class FakeCli:
        def __init__(self, *a, **k): self.persona_required = True
        def pending_persona(self, resolve=True):
            assert resolve is True, 'link 는 반드시 해석해야 한다'
            return {'persona_url': 'https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_L',
                    'inquiry': 'inq_L'}
        def authenticate(self):
            raise AssertionError('살아있는 challenge 가 있으면 새로 발급하면 안 된다')
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCli)

    j = app_mod.app.test_client().post('/api/account/wqb-persona-link').get_json()
    assert j['ok'] is True and j['inquiry'] == 'inq_L', j
    assert 'withpersona.com' in j['persona_url'], j


def test_persona_link_mints_fresh_challenge_when_pending_is_gone(monkeypatch):
    """challenge 가 죽어 pending_persona 가 None 을 주면(410 Gone → .pending 삭제),
    그 자리에서 한 번만 새 challenge 를 발급하고 링크를 돌려준다."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))

    calls = []

    class FakeCli:
        def __init__(self, *a, **k): self.persona_required = False
        def pending_persona(self, resolve=True):
            calls.append('pending')
            if not self.persona_required:
                return None
            return {'persona_url': 'https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_N',
                    'inquiry': 'inq_N'}
        def authenticate(self):
            calls.append('auth')
            self.persona_required = True
            return False
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCli)

    j = app_mod.app.test_client().post('/api/account/wqb-persona-link').get_json()
    assert j['ok'] is True and j['inquiry'] == 'inq_N', j
    assert calls == ['pending', 'auth', 'pending'], calls   # 발급은 정확히 한 번


def test_persona_link_does_not_mint_on_transient_resolve_failure(monkeypatch):
    """해석이 일시 실패(빈 URL)면 challenge 는 살아 있다 — 새로 발급하지 말고 재시도를 안내한다.
    (여기서 발급하면 사용자가 방금 연 인증 페이지가 죽는다.)"""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))

    class FakeCli:
        def __init__(self, *a, **k): self.persona_required = True
        def pending_persona(self, resolve=True):
            return {'persona_url': '', 'inquiry': 'inq_T'}
        def authenticate(self):
            raise AssertionError('일시 실패에 새 challenge 를 발급하면 안 된다')
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCli)

    j = app_mod.app.test_client().post('/api/account/wqb-persona-link').get_json()
    assert j['ok'] is False and j['reason'] == 'resolving', j


# ── force=true: 사용자가 '인증 링크 재발급' 을 누른 경우 ──────────────────────

def test_persona_link_force_mints_even_when_challenge_is_live(monkeypatch):
    """열어 둔 인증 페이지가 'session expired' 로 죽으면 WQB 쪽 challenge 는 살아 있어
    자동 재발급 조건에 안 걸린다 — 사용자가 스스로 빠져나올 수 있어야 한다(2026-07-22 사장 지시).
    force 면 살아있는 challenge 라도 버리고 mint_challenge() 로 새로 발급한다."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))

    calls = []

    class FakeCliLive:
        def __init__(self, *a, **k): self.persona_required = True
        def _read_pending(self):
            # 살아있는(마커 아닌) challenge — force 가 없으면 그대로 해석되던 상태.
            return {'persona_url': 'https://api/persona?inquiry=OLD', 'cookies': {'c': '1'}}
        def mint_challenge(self):
            calls.append('mint')
            self.persona_required = True
            return False
        def authenticate(self):
            raise AssertionError('force 는 mint_challenge() 를 타야 한다')
        def pending_persona(self, resolve=True):
            calls.append('resolve')
            assert 'mint' in calls, 'force 인데 옛 challenge 를 그대로 해석했다'
            return {'persona_url': 'https://inquiry.withpersona.com/verify?x=1', 'inquiry': 'FRESH'}
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliLive)

    cl = app_mod.app.test_client()
    j = cl.post('/api/account/wqb-persona-link', json={'force': True}).get_json()
    assert calls == ['mint', 'resolve'], calls
    assert j.get('ok') is True and j.get('inquiry') == 'FRESH', j
    assert 'withpersona.com' in (j.get('persona_url') or ''), j


def test_persona_link_without_force_keeps_live_challenge(monkeypatch):
    """force 가 없으면 살아있는 challenge 를 절대 갈아치우지 않는다 — 재발급은
    사용자가 명시적으로 누를 때만(그 순간 열려 있는 인증 페이지가 죽는다)."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))

    class FakeCliLive:
        def __init__(self, *a, **k): self.persona_required = True
        def _read_pending(self):
            return {'persona_url': 'https://api/persona?inquiry=OLD', 'cookies': {'c': '1'}}
        def mint_challenge(self):
            raise AssertionError('force 없이 재발급하면 안 된다')
        def authenticate(self):
            raise AssertionError('살아있는 challenge 를 새로 발급하면 안 된다')
        def pending_persona(self, resolve=True):
            return {'persona_url': 'https://inquiry.withpersona.com/verify?x=1', 'inquiry': 'OLD'}
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliLive)

    j = app_mod.app.test_client().post(
        '/api/account/wqb-persona-link', json={}).get_json()
    assert j.get('ok') is True and j.get('inquiry') == 'OLD', j


def test_persona_link_force_reports_rate_limit(monkeypatch):
    """재발급은 POST /authentication 1회를 쓴다 — 분당 5회 한도에 걸리면 그대로 알린다."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))

    class FakeCli429:
        def __init__(self, *a, **k):
            self.persona_required = False
            self.last_auth_status_code = 429
        def _read_pending(self): return None
        def mint_challenge(self): return False
        def pending_persona(self, resolve=True):
            raise AssertionError('throttle 상태에서 해석까지 가면 안 된다')
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCli429)

    j = app_mod.app.test_client().post(
        '/api/account/wqb-persona-link', json={'force': True}).get_json()
    assert j.get('ok') is False and j.get('reason') == 'rate_limited', j


def test_persona_link_requires_login(monkeypatch):
    import server.app as app_mod
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: None)
    r = app_mod.app.test_client().post('/api/account/wqb-persona-link')
    assert r.status_code == 401


def test_persona_watch_requires_login(monkeypatch):
    import server.app as app_mod
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: None)
    r = app_mod.app.test_client().get('/api/account/wqb-persona-watch')
    assert r.status_code == 401


def test_persona_status_rate_limited(monkeypatch):
    """429 rate-limit 시 rate_limited=True 와 account_type 이 함께 반환되어야 한다."""
    import server.app as app_mod
    import server.wqb_api as wqb_api
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 3)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))
    monkeypatch.setattr(app_mod._db, 'get_account_type', lambda uid: 'research_consultant')
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api',
        lambda u, p: {'ok': False, 'reason': 'wqb_rate_limited',
                      'detail': 'WQB API 인증 호출 한도(분당 5회) 초과 — 1분 후 다시 시도하세요.'})
    class FakeCliNoSession:
        def __init__(self, *a, **k):
            self.persona_required = False
            self.last_auth_status_code = 429
        def _load_session(self): return False
        def _session_valid(self): return False
        def pending_persona(self): return None
        def authenticate(self): return False
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliNoSession)
    cl = app_mod.app.test_client()
    r = cl.get('/api/account/wqb-persona-status')
    j = r.get_json()
    assert j.get('rate_limited') is True, f"expected rate_limited=True, got {j}"
    assert j.get('account_type') == 'research_consultant', f"missing account_type: {j}"
    assert j.get('persona_required') is False
