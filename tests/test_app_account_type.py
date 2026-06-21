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

    def fake_validate_login(u, p, g, account_type='standard'):
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

    def fake_validate(u, p, g, account_type='standard'):
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

def test_persona_status_and_complete(monkeypatch):
    import server.app as app_mod
    monkeypatch.setattr(app_mod, '_current_user_id', lambda: 2)
    monkeypatch.setattr(app_mod._db, 'get_user_credentials', lambda uid: ('e', 'p', 'g'))
    monkeypatch.setattr(app_mod._auth, 'validate_wqb_api',
        lambda u, p: {'ok': False, 'reason': 'wqb_persona_required', 'persona_url': 'https://x/persona?inquiry=Z'})
    cl = app_mod.app.test_client()
    r = cl.get('/api/account/wqb-persona-status')
    j = r.get_json()
    assert j['persona_required'] is True and 'inquiry=Z' in j['persona_url']
    # complete: monkeypatch the client factory used by the endpoint
    import server.wqb_api as wqb_api
    class FakeCli:
        def __init__(self, *a, **k): pass
        def complete_persona(self): return True
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeCli)
    r2 = cl.post('/api/account/wqb-persona-complete')
    assert r2.get_json()['ok'] is True
