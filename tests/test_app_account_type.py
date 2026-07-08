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
        def pending_persona(self):
            return {'persona_url': 'https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_Z',
                    'inquiry': 'inq_Z'} if self.persona_required else None
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
    assert j['persona_required'] is True and 'withpersona.com' in j['persona_url']
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


def test_persona_status_pending_returns_url_without_post(monkeypatch):
    """저장된 세션이 만료됐지만 미완료 persona challenge(.pending)가 있으면,
    상태조회는 그 URL 을 반환하고 절대 validate_wqb_api(POST /authentication)를
    호출하지 않아야 한다. (passive 조회가 biometric throttle 를 재무장시키던 버그 방지.)"""
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
        def pending_persona(self):
            return {'persona_url': 'https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_P',
                    'inquiry': 'inq_P'}
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCliPending)

    cl = app_mod.app.test_client()
    r = cl.get('/api/account/wqb-persona-status')
    j = r.get_json()
    assert j.get('persona_required') is True, j
    assert 'withpersona.com' in j.get('persona_url', ''), j
    assert j.get('inquiry') == 'inq_P', j
    assert j.get('rate_limited') is not True, j
    assert j.get('account_type') == 'research_consultant', j


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
